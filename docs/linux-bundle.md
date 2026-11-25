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
`build_output/installer/fmri_tui-<version>-linux-x86_64.tar.gz` (about 95 MB).

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

No sudo: the app goes to `~/.local/share/fmri_local_process/` and the commands to `~/.local/bin/`.
Open a new terminal afterwards. The commands are `fmri_tui`, `fmri_convert_to_bids`,
`fmri_preprocess`, `fmri_mriqc`, `fmri_behav_check`, `fmri_behav_fix`, `fmri_behav_events`,
`fmri_bids_check`, `fmri_bids_fix`, `fmri_folders` and `fmri_generate_sample_data`: enough to work without the terminal app (see
the README's Linux section).

Before the first run, on the server:

- **Docker** (for C-PAC, MRIQC and defacing): your user must be allowed to use it (`docker info`
  works without sudo).
- **Images:** `docker/setup_docker_image.sh --defacer freesurfer --mriqc` (in the unpacked folder)
  downloads C-PAC and MRIQC and builds the defacer, as the Windows installer does; `--help` shows the
  other choices. Without a defacer, the T1w scans are converted but **not defaced**, and the Setup tab
  says so.
- **Home folder mounted `noexec`?** Programs can't run from there: install with
  `XDG_DATA_HOME=/some/other/folder ./install.sh`.
