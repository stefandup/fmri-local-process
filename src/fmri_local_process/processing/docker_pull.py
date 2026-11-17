"""Download a Docker image with a progress measure, for the C-PAC and MRIQC images the
toolbox runs; and remove one again.

The download asks Docker's engine directly (its API, over the same local pipe or socket the
`docker` command uses): it reports the bytes downloaded per layer, so progress moves
smoothly. When the engine can't be reached that way, it falls back to `docker pull`, which
prints no byte counts when its output isn't a terminal, only each layer's milestones; with
the layer sizes from the registry (`docker manifest inspect`), progress then moves a layer at
a time, in proportion to the bytes. Without sizes it counts layers.
"""

import io
import json
import re
import socket
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, TextIO
from urllib.parse import quote

LAYER_ID = re.compile(r"[0-9a-f]{12}")
LAYER_LINE = re.compile(r"^([0-9a-f]{12}): (.+)$")
SEEN, DOWNLOADED, DONE = "seen", "downloaded", "done"
STAGE_ORDER = (SEEN, DOWNLOADED, DONE)
STATUS_STAGE = {
    "Pulling fs layer": SEEN,
    "Waiting": SEEN,
    "Download complete": DOWNLOADED,
    "Pull complete": DONE,
    "Already exists": DONE,
}
DOWNLOADED_SHARE = 0.85  # downloading is most of the time; unpacking ('extracting') the rest
PLATFORM = ("linux", "amd64")


def run_logged(cmd: list[str], log_path: Path, on_line: Callable[[str], None] | None = None) -> int:
    """Run cmd, writing its console output to log_path and passing each line to on_line."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with (
        log_path.open("w", encoding="utf-8") as out,
        subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        ) as proc,
    ):
        assert proc.stdout is not None  # stdout=PIPE guarantees it
        for line in proc.stdout:
            out.write(line)
            out.flush()  # readable while it runs
            if on_line is not None:
                on_line(line.rstrip("\n"))
    return proc.returncode


def parse_layer_sizes(manifest_json: str) -> dict[str, int]:
    """{12-character layer id, as docker pull prints it: compressed size in bytes} from
    `docker manifest inspect --verbose` (one platform's entry, or a list of them)."""
    data = json.loads(manifest_json)
    entries = data if isinstance(data, list) else [data]
    chosen = next(
        (
            entry
            for entry in entries
            if (entry.get("Descriptor", {}).get("platform") or {}).get("os") == PLATFORM[0]
            and entry["Descriptor"]["platform"].get("architecture") == PLATFORM[1]
        ),
        entries[0] if entries else {},
    )
    manifest = chosen.get("SchemaV2Manifest") or chosen.get("OCIManifest") or {}
    sizes = {}
    for layer in manifest.get("layers", []):
        digest = str(layer.get("digest", ""))
        if ":" in digest:
            sizes[digest.split(":", 1)[1][:12]] = int(layer.get("size", 0))
    return sizes


def layer_sizes(docker: Path, image: str) -> dict[str, int]:
    """The image's layer sizes from its registry; {} when they can't be read."""
    try:
        result = subprocess.run(
            [str(docker), "manifest", "inspect", "--verbose", image],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return parse_layer_sizes(result.stdout) if result.returncode == 0 else {}
    except (subprocess.TimeoutExpired, OSError, ValueError, KeyError, AttributeError):
        return {}


@dataclass
class PullProgress:
    sizes: dict[str, int] = field(default_factory=dict)  # layer id -> bytes ({} unknown)
    stages: dict[str, str] = field(default_factory=dict)  # layer id -> SEEN/DOWNLOADED/DONE
    got: dict[str, int] = field(default_factory=dict)  # layer id -> bytes downloaded so far

    def feed(self, line: str) -> bool:
        """Take one line of `docker pull` output; True when it moved a layer on."""
        match = LAYER_LINE.match(line.strip())
        return match is not None and self.advance(*match.groups())

    def feed_event(self, event: dict) -> bool:
        """Take one of the engine's pull messages ({"id", "status", "progressDetail"}); True
        when it moved a layer on or reported more bytes."""
        layer, status = str(event.get("id", "")), str(event.get("status", ""))
        if not LAYER_ID.fullmatch(layer):
            return False  # about the image as a whole ('Pulling from ...', 'Digest: ...')
        changed = self.advance(layer, status)
        detail = event.get("progressDetail") or {}
        if status == "Downloading" and isinstance(detail.get("current"), int):
            self.got[layer] = detail["current"]
            if isinstance(detail.get("total"), int) and detail["total"] > 0:
                self.sizes.setdefault(layer, detail["total"])
            changed = True
        return changed

    def advance(self, layer: str, status: str) -> bool:
        stage = STATUS_STAGE.get(status, SEEN)  # 'Downloading', 'Verifying Checksum' etc.
        current = self.stages.get(layer)
        if current is not None and STAGE_ORDER.index(stage) <= STAGE_ORDER.index(current):
            return False  # nothing new, and never backwards
        self.stages[layer] = stage
        return True

    def layers(self) -> list[str]:
        return sorted(set(self.sizes) | set(self.stages))

    def layer_share(self, layer: str) -> float:
        """0..1 of this layer: its bytes downloaded so far count towards DOWNLOADED_SHARE."""
        stage = self.stages.get(layer, SEEN)
        if stage == DONE:
            return 1.0
        if stage == DOWNLOADED:
            return DOWNLOADED_SHARE
        size = self.sizes.get(layer, 0)
        return DOWNLOADED_SHARE * min(1.0, self.got.get(layer, 0) / size) if size else 0.0

    def fraction(self) -> float:
        """0..1 of the image."""
        layers = self.layers()
        if not layers:
            return 0.0
        total = sum(self.sizes.get(layer, 0) for layer in layers)
        if total <= 0:  # sizes unknown: every layer counts the same
            return sum(map(self.layer_share, layers)) / len(layers)
        return sum(self.sizes.get(layer, 0) * self.layer_share(layer) for layer in layers) / total

    def downloaded_bytes(self) -> int:
        return sum(
            size if self.stages.get(layer) in (DOWNLOADED, DONE) else self.got.get(layer, 0)
            for layer, size in self.sizes.items()
        )

    def text(self) -> str:
        layers = self.layers()
        done = sum(self.stages.get(layer) == DONE for layer in layers)
        total = sum(self.sizes.values())
        if total <= 0:
            return f"{done} of {len(layers)} layers done"
        unit, scale = ("GB", 1024**3) if total >= 1024**3 else ("MB", 1024**2)
        return (
            f"{self.downloaded_bytes() / scale:.1f} of {total / scale:.1f} {unit} downloaded, "
            f"{done} of {len(layers)} layers unpacked"
        )


# ---- Docker's engine, asked directly ----------------------------------------------------------


class EngineError(RuntimeError):
    """The engine answered, but with an error (e.g. no such image)."""


def engine_address(docker: Path) -> str:
    """Where the current Docker context's engine listens, e.g. 'npipe:////./pipe/...' or
    'unix:///var/run/docker.sock'; '' when docker can't say."""
    try:
        result = subprocess.run(
            [str(docker), "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


@contextmanager
def engine_connection(address: str) -> Iterator[tuple[BinaryIO, Callable[[bytes], object]]]:
    """(reader, write) to the engine at address: a Windows named pipe or a Unix socket.
    Raises OSError for anything else (a remote engine over TCP needs TLS: `docker pull` then)."""
    if address.startswith("npipe://"):
        pipe = "\\\\.\\pipe\\" + address.split("/pipe/", 1)[1]
        with open(pipe, "r+b", buffering=0) as raw:  # unbuffered: a pipe can't seek
            yield io.BufferedReader(raw), raw.write
    elif address.startswith("unix://"):
        unix = getattr(socket, "AF_UNIX")  # noqa: B009 -- absent from Windows' type stubs
        with socket.socket(unix, socket.SOCK_STREAM) as sock:
            sock.connect(address.removeprefix("unix://"))
            with sock.makefile("rb") as reader:
                yield reader, sock.sendall
    else:
        raise OSError(f"can't reach the Docker engine at {address or '(unknown)'} directly")


def read_json_stream(reader: BinaryIO) -> Iterator[dict]:
    """The JSON messages of one HTTP response (chunked, as the engine streams them)."""
    status = reader.readline().split()
    code = int(status[1]) if len(status) > 1 else 0
    headers = {}
    while line := reader.readline().strip():
        name, _, value = line.decode("latin-1").partition(":")
        headers[name.strip().lower()] = value.strip()
    if headers.get("transfer-encoding", "").lower() == "chunked":
        chunks = iter(lambda: read_chunk(reader), b"")
    else:
        chunks = iter([reader.read(int(headers.get("content-length", "0")))])
    pending = b""
    for chunk in chunks:
        pending += chunk
        *complete, pending = pending.split(b"\n")
        for piece in filter(None, map(bytes.strip, complete)):
            message = json.loads(piece)
            if code != 200:
                raise EngineError(message.get("message", f"HTTP {code}"))
            yield message
    if pending.strip():
        message = json.loads(pending)
        if code != 200:
            raise EngineError(message.get("message", f"HTTP {code}"))
        yield message


def read_chunk(reader: BinaryIO) -> bytes:
    size = int(reader.readline().split(b";")[0].strip() or b"0", 16)
    data = reader.read(size) if size else b""
    reader.readline()  # the CRLF after the chunk
    return data


def split_image(image: str) -> tuple[str, str]:
    """'nipreps/mriqc:24.0.2' -> ('nipreps/mriqc', '24.0.2'); a registry port isn't a tag."""
    name, _, tag = image.rpartition(":")
    if not name or "/" in tag:
        return image, "latest"
    return name, tag


def engine_pull(address: str, image: str, on_event: Callable[[dict], None]) -> None:
    """Pull image through the engine's API, passing on each progress message. Raises
    EngineError when the pull fails, OSError when the engine can't be reached."""
    name, tag = split_image(image)
    request = (
        f"POST /images/create?fromImage={quote(name, safe='')}&tag={quote(tag, safe='')} "
        "HTTP/1.1\r\nHost: docker\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    )
    with engine_connection(address) as (reader, write):
        write(request.encode())
        for event in read_json_stream(reader):
            if "error" in event:
                raise EngineError(event.get("errorDetail", {}).get("message") or event["error"])
            on_event(event)


def log_event(log: TextIO, event: dict, last: dict[str, str]) -> None:
    """One line per change of a layer's status, as `docker pull` prints them."""
    layer, status = str(event.get("id", "")), str(event.get("status", ""))
    if last.get(layer) != status:
        last[layer] = status
        log.write(f"{layer}: {status}\n" if layer else f"{status}\n")
        log.flush()


def remove_image(docker: Path, image: str) -> str | None:
    """`docker rmi image`, freeing its disk space (layers another image shares stay). None
    when it worked, else docker's reason (e.g. a container of it is still running)."""
    try:
        result = subprocess.run(
            [str(docker), "rmi", image], capture_output=True, text=True, timeout=300
        )
    except subprocess.TimeoutExpired:
        return "'docker rmi' didn't finish in time."
    if result.returncode == 0:
        return None
    return (result.stderr or result.stdout).strip() or f"'docker rmi' failed ({result.returncode})"


def remove_partial(docker: Path, image: str) -> str | None:
    """Clear what an unfinished download left: the image, if docker registered it, and
    dangling (untagged, unused) images. Docker's own clean-up removes unreferenced layers
    then. None when it worked, else docker's reason."""
    error = remove_image(docker, image)
    if error is not None and "No such image" not in error:
        return error
    try:
        result = subprocess.run(
            [str(docker), "image", "prune", "--force"], capture_output=True, text=True, timeout=300
        )
    except subprocess.TimeoutExpired:
        return "'docker image prune' didn't finish in time."
    if result.returncode != 0:
        return (result.stderr or result.stdout).strip() or "'docker image prune' failed"
    return None


PROGRESS_INTERVAL_S = 0.5  # the engine reports bytes many times a second


def pull_image(
    docker: Path, image: str, log_path: Path, on_progress: Callable[[PullProgress], None]
) -> int:
    """Download image, calling on_progress as it moves on; 0 when it worked. Through the
    engine (bytes), else `docker pull` (layers). Layers already downloaded are kept when it
    fails, so pulling again resumes. The log's last line says why it failed."""
    progress = PullProgress(layer_sizes(docker, image))
    on_progress(progress)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    shown = 0.0
    last: dict[str, str] = {}
    with log_path.open("w", encoding="utf-8") as log:

        def on_event(event: dict) -> None:
            nonlocal shown
            log_event(log, event, last)
            stage_before = dict(progress.stages)
            if progress.feed_event(event) and (
                progress.stages != stage_before or time.monotonic() - shown > PROGRESS_INTERVAL_S
            ):
                shown = time.monotonic()
                on_progress(progress)

        try:
            engine_pull(engine_address(docker), image, on_event)
            return 0
        except EngineError as e:
            log.write(f"{e}\n")
            return 1
        except (OSError, ValueError) as e:  # can't ask the engine directly: docker pull
            log.write(f"Engine not reachable directly ({e}); using docker pull.\n")

    def on_line(line: str) -> None:
        if progress.feed(line):
            on_progress(progress)

    return run_logged([str(docker), "pull", image], log_path, on_line)
