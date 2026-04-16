# Changelog

All notable changes to fastagent are documented in this file.

## 0.4.0

### Bug fixes

- **Socket timeout not cleared after connect probe.** The 2-second timeout
  used to test the local forwarding socket was never cleared, so any module
  whose remote work took longer than 2 seconds (e.g. `ufw` reloading
  iptables) failed with "timed out". The timeout is now cleared to blocking
  after a successful connect, matching SSH behavior.

- **Local socket name collision between become and non-become modes.**
  A stale forwarding socket from a non-become run could be reused by a
  become run, forwarding to the wrong remote daemon. Socket paths now
  include the become user.

- **`runuser` called when become was disabled.** `exec_command` wrapped
  every command in `runuser -u <user>` when become was off, but the daemon
  runs as the connecting user in that case and `runuser` requires root.

- **macOS fork + SSL crash during binary download.** `urllib.urlopen` inside
  Ansible's forked worker process triggered a macOS fork-safety crash.
  Switched to `curl` in a subprocess.

### Other changes

- Packaged as an Ansible Collection (`kevinburke.fastagent`), installable
  via `ansible-galaxy collection install`.
- Published to Ansible Galaxy.
- Added `meta/runtime.yml` (`requires_ansible: ">=2.12.0"`).
- Added `scripts/release.sh` for one-command releases (build, tag, GitHub
  release, Galaxy publish).
- Added `scripts/check-versions.sh` to catch version mismatches across
  `fastagent.go`, `galaxy.yml`, and `plugins/connection/fastagent.py`.
- README rewritten around third-party install and verification.
- Clarified that unqualified module names are required for the speedup;
  the persistent connection alone does not outperform SSH ControlMaster.
- CI: fixed five pre-existing breakages, added version-check and
  collection-layout test steps, added connection plugin regression tests.

## 0.3.0

Initial public release. Persistent Go agent over SSH with Unix socket
forwarding, action plugin overrides for file/copy/template/service/package,
and `--allow-user` for socket access control.
