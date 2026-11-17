"""Download progress of `docker pull`, from synthetic docker output and manifests (no Docker)."""

import io
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from fmri_local_process.processing import docker_pull as dp

BIG, SMALL = "a" * 64, "b" * 64  # layer digests (hex); docker prints their first 12 characters


def manifest(platform: tuple[str, str] = ("linux", "amd64")) -> dict:
    return {
        "Descriptor": {"platform": {"os": platform[0], "architecture": platform[1]}},
        "SchemaV2Manifest": {
            "layers": [
                {"digest": f"sha256:{BIG}", "size": 3 * 1024**3},
                {"digest": f"sha256:{SMALL}", "size": 1 * 1024**3},
            ]
        },
    }


def test_layer_sizes_from_one_or_several_platforms() -> None:
    expected = {BIG[:12]: 3 * 1024**3, SMALL[:12]: 1 * 1024**3}
    assert dp.parse_layer_sizes(json.dumps(manifest())) == expected
    arm = manifest(("linux", "arm64"))
    arm["SchemaV2Manifest"]["layers"] = []
    assert dp.parse_layer_sizes(json.dumps([arm, manifest()])) == expected  # amd64 picked


def test_progress_follows_the_layers_by_size() -> None:
    progress = dp.PullProgress({BIG[:12]: 3 * 1024**3, SMALL[:12]: 1 * 1024**3})
    assert not progress.feed("24.0.2: Pulling from nipreps/mriqc")  # not a layer line
    assert progress.feed(f"{BIG[:12]}: Pulling fs layer")
    assert progress.fraction() == 0.0
    assert progress.feed(f"{SMALL[:12]}: Already exists")  # shared with another image
    assert progress.fraction() == pytest.approx(0.25)
    progress.feed(f"{BIG[:12]}: Verifying Checksum")
    progress.feed(f"{BIG[:12]}: Download complete")
    assert progress.fraction() == pytest.approx(0.25 + 0.75 * dp.DOWNLOADED_SHARE)
    assert progress.text().startswith("4.0 of 4.0 GB downloaded, 1 of 2 layers")
    assert progress.feed(f"{BIG[:12]}: Pull complete")
    assert not progress.feed(f"{BIG[:12]}: Download complete")  # never backwards
    assert progress.fraction() == 1.0


def test_progress_counts_layers_without_sizes() -> None:
    progress = dp.PullProgress()
    for line in ("111111111111: Pulling fs layer", "222222222222: Waiting"):
        progress.feed(line)
    progress.feed("111111111111: Pull complete")
    assert progress.fraction() == 0.5
    assert progress.text() == "1 of 2 layers done"


def chunked(*chunks: bytes, status: str = "200 OK") -> io.BytesIO:
    """An HTTP response as the engine streams it, in these chunks."""
    body = b"".join(b"%x\r\n%s\r\n" % (len(c), c) for c in chunks) + b"0\r\n\r\n"
    head = f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
    return io.BytesIO(f"{head}Transfer-Encoding: chunked\r\n\r\n".encode() + body)


def test_json_stream_across_chunks_and_errors() -> None:
    reader = chunked(
        b'{"status":"Pulling from x"}\r\n{"id":"1111', b'11111111","status":"Waiting"}\r\n'
    )
    assert list(dp.read_json_stream(reader)) == [
        {"status": "Pulling from x"},
        {"id": "111111111111", "status": "Waiting"},
    ]
    with pytest.raises(dp.EngineError, match="manifest unknown"):
        list(
            dp.read_json_stream(chunked(b'{"message":"manifest unknown"}', status="404 Not Found"))
        )


def test_engine_messages_give_byte_progress() -> None:
    progress = dp.PullProgress({BIG[:12]: 3 * 1024**3, SMALL[:12]: 1 * 1024**3})
    assert not progress.feed_event({"status": "Pulling from nipreps/mriqc", "id": "24.0.2"})
    progress.feed_event({"id": SMALL[:12], "status": "Already exists"})
    half = {"current": 3 * 1024**3 // 2, "total": 3 * 1024**3}
    assert progress.feed_event({"id": BIG[:12], "status": "Downloading", "progressDetail": half})
    assert progress.fraction() == pytest.approx(0.25 + 0.75 * 0.5 * dp.DOWNLOADED_SHARE)
    assert progress.text().startswith("2.5 of 4.0 GB downloaded")
    assert dp.split_image("nipreps/mriqc:24.0.2") == ("nipreps/mriqc", "24.0.2")
    assert dp.split_image("localhost:5000/x") == ("localhost:5000/x", "latest")


def test_engine_pull_sends_the_request_and_raises_its_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[bytes] = []
    response = chunked(b'{"id":"111111111111","status":"Waiting"}\n{"error":"TLS timeout"}\n')

    @contextmanager
    def fake_connection(address: str) -> Iterator[tuple[io.BytesIO, Callable]]:
        assert address == "npipe:////./pipe/engine"
        yield response, sent.append

    monkeypatch.setattr(dp, "engine_connection", fake_connection)
    events: list[dict] = []
    with pytest.raises(dp.EngineError, match="TLS timeout"):
        dp.engine_pull("npipe:////./pipe/engine", "nipreps/mriqc:24.0.2", events.append)
    assert sent[0].startswith(b"POST /images/create?fromImage=nipreps%2Fmriqc&tag=24.0.2 ")
    assert events == [{"id": "111111111111", "status": "Waiting"}]


def test_pull_image_reports_each_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dp, "layer_sizes", lambda docker, image: {})
    monkeypatch.setattr(dp, "engine_address", lambda docker: "")  # engine not reachable

    def fake_run(cmd: list[str], log_path: Path, on_line=None) -> int:  # noqa: ANN001
        assert cmd[-2:] == ["pull", "some/image:1"] and on_line is not None
        for line in ("111111111111: Pulling fs layer", "111111111111: Pull complete", "Digest: x"):
            on_line(line)
        return 0

    monkeypatch.setattr(dp, "run_logged", fake_run)
    seen: list[float] = []
    code = dp.pull_image(Path("docker"), "some/image:1", tmp_path / "log.txt",
                         lambda progress: seen.append(progress.fraction()))  # fmt: skip
    assert code == 0 and seen == [0.0, 0.0, 1.0]
