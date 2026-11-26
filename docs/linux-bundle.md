# The Linux bundle

One `.tar.gz` for headless servers and the HPC: [the terminal app](tui.md) and the command-line
tools, with their own Python and dcm2niix. No Qt, and no Python needed on the server.

## Build it

```bash
./build.sh --tui      # needs Docker; on Linux or in WSL
```

It builds in a `rockylinux:8` container (glibc 2.28), so the result runs on RHEL/Rocky 8 and later,
Ubuntu 20.04 and later, and Debian 10 and later, on 64-bit Intel/AMD. Only the files git tracks go
into the container, never data. The first build takes about 5 minutes, later ones less (downloads
and dcm2niix are kept in `build_output/cache/`). The result is
`build_output/installer/fmri_tui-<version>-linux-x86_64.tar.gz` (about 200 MB).

dcm2niix is built from source, at the version in `tools/dcm2niix_version.txt`: the release's own
binary needs a newer glibc than RHEL/Rocky 8 has.

## Check it

```bash
packaging/linux/check_bundle.sh      # needs Docker and tmux
```

Installs it as an ordinary user in clean `ubuntu:22.04`, `rockylinux:8` and `debian:12` containers,
runs every command, opens and quits the terminal app (also with a plain C locale), and uninstalls it.

## Install it on the server

```bash
scp fmri_tui-*-linux-x86_64.tar.gz myserver:
ssh myserver
tar -xzf fmri_tui-*-linux-x86_64.tar.gz
./fmri_tui-*/install.sh              # ./install.sh --uninstall removes it
```

Or build and install in one go, for your own user on the server:

```bash
./build.sh --tui --remote-install myserver    # a Host from ~/.ssh/config, or user@server
```

It checks the SSH connection before the build, then streams the `.tar.gz` over one connection,
unpacks it in your home folder there (`~/fmri_tui-<version>/`, keep it for `--uninstall`) and
runs `install.sh`. An upgrade replaces the earlier version: `install.sh` swaps the toolbox folder, and
earlier `~/fmri_tui-*/` folders are removed. The Docker images stay: the toolbox finds them by name,
so they aren't downloaded again.

No sudo: every tool goes into one toolbox folder, `~/.local/opt/fmri_local_process/`, and that folder is
added to your PATH (as the Windows installer does). Open a new terminal afterwards. The commands are `fmri_tui`, `fmri_convert_to_bids`,
`fmri_preprocess`, `fmri_mriqc`, `fmri_behav_events`, `fmri_behav_check`, `fmri_bids_check` and
`fmri_generate_sample_data`.

Before the first run, on the server:

- **Docker** (for C-PAC, MRIQC and defacing): your user must be allowed to use it (`docker info`
  works without sudo).
- **Images:** `~/.local/opt/fmri_local_process/docker/setup_docker_image.sh --defacer freesurfer --mriqc`
  downloads C-PAC and MRIQC and builds the defacer, as the Windows installer does; `--help` shows the
  other choices. Without a defacer, the T1w scans are converted but **not defaced**, and the Setup tab
  says so.
- **Home folder mounted `noexec`?** Programs can't run from there: install with
  `./install.sh /some/other/folder` (and `./install.sh --uninstall /some/other/folder`).

## Releases and updates

Pushing a tag builds the bundle on GitHub (`.github/workflows/release.yml`, the same `build.sh --tui`)
and publishes it as a release:

```bash
git tag v0.2.0 && git push origin v0.2.0
```

The release has the bundle (also as `fmri_tui-linux-x86_64.tar.gz`, a name that stays the same),
`fmri_update`, `VERSION` and `SHA256SUMS`. Users install and update with `fmri_update`, a small script
that is also in the bundle, so it is on the PATH and updates itself:

```bash
curl -fsSLO https://github.com/stefandup/fmri-local-process/releases/latest/download/fmri_update
bash fmri_update && rm fmri_update   # first time; then log in again

fmri_update            # the latest release, if newer than the installed one
fmri_update --check    # only say whether there is a newer one
fmri_update v0.2.0     # a given release, e.g. to go back one
fmri_update --file fmri_tui-…-linux-x86_64.tar.gz   # a local bundle, without the internet
fmri_update --uninstall
```

It downloads with curl (or wget), checks the download against `SHA256SUMS` and runs the bundle's own
`install.sh`, so an update replaces the earlier version. The Docker images are kept: the toolbox finds
them by name, and they are shared by every user on the server. `./build.sh --tui --remote-install`
stays for trying a local build on a server.
