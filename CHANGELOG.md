# Changelog

All notable changes to fastagent are documented in this file.

## 0.5.7 — April 20, 2026

### Bug fixes

- **Suppress Ansible's redundant become wrap for every task, not just
  action overrides.** 0.5.5 made the agent handle become itself, and
  flipped `play_context.become = False` to stop Ansible from also
  wrapping commands. That worked for our action-plugin overrides
  (`file`, `copy`, `template`, ...) because those bypass
  `ActionBase._low_level_execute_command`, but modules without an
  override (e.g. `community.general.git_config`) still went through
  `_low_level_execute_command`, which checks `self._connection.become`
  — the *plugin object*, not the play_context flag. The ansible-side
  wrap was therefore still firing for those tasks, producing a
  `sudo -u returns sh -c …` outside our agent's own `sudo` wrap. On
  hosts where the target user isn't in `/etc/sudoers` (e.g. the
  dedicated `returns` app user) this failed with `returns is not in
  the sudoers file`.

  Fixed by overriding `Connection.set_become_plugin(plugin)` so we
  never attach the plugin to `self.become`. We capture the target
  user from `plugin.get_option("become_user")` for our own Exec RPC
  and leave `self.become = None`, which suppresses the
  `ActionBase._low_level_execute_command` wrap for every task
  regardless of whether there's an action override. The non-sudo
  become methods (`su`, `runas`, ...) still fall through to
  Ansible's built-in wrap, since the agent only knows how to drive
  `sudo`.

## 0.5.6 — April 20, 2026

### Bug fixes

- **Detect controller/daemon version skew.** Go's JSON decoder
  silently ignores unknown fields, so an older daemon that was still
  running on a host would accept RPCs that include new fields (the
  `become_user` field added in 0.5.5, for instance) and just drop
  them — running the command as root instead of as the requested
  user. Two defenses now make this impossible:

  1. The version is baked into the socket path
     (`/tmp/fastagent-root-{version}.sock` when become is in effect,
     `/tmp/fastagent-{version}.sock` otherwise). A 0.5.6 controller
     cannot reach a 0.5.5 daemon's socket, so `test -S <sock>` is an
     accurate proxy for "a daemon of the right version is running."
     The bootstrap path kills any fastagent daemon (any version) on
     the host before starting a fresh one, so the old daemon doesn't
     hang around consuming resources.

  2. `FastAgentClient.hello()` now compares the daemon's returned
     version against the expected version and raises
     `FastAgentVersionMismatch` on skew. `_try_local_socket` treats
     this like any other probe failure — the local socket is torn
     down and the caller falls through to the bootstrap path. This
     is belt-and-braces for cases where someone's hand-edited the
     socket path or the paths otherwise collide.

## 0.5.5 — April 20, 2026

### Bug fixes

- **Become now runs inside the agent.** 0.5.4 fixed the "user not in
  sudoers" regression by running the daemon as root, but that caused
  a new one: `command`/`shell` tasks with `become_user: X` ran as
  root on the remote side, which tripped git's CVE-2022-24765
  "dubious ownership" check against any repo owned by `X`, and in
  general diverged from ansible's semantics (files created owned by
  root, commands seeing root's environment). The controller now
  flips `play_context.become = False` to suppress ansible's own
  `sudo -u X sh -c …` wrapper and passes `become_user` through the
  `Exec` RPC instead. The agent wraps the command with
  `sudo -H -n -u X --` at dispatch time, so the target command runs
  as `X` with `X`'s HOME and environment — matching what SSH +
  ansible's classic become path would do.

  When `become_user` is `root` (the default), the daemon is already
  root, so the sudo wrap is skipped entirely.

### Security

- **Stat and ReadFile RPCs reject `become_user`.** Running these as
  root on behalf of a non-root `become_user` would be a permission
  leak (root can see files the target user can't). The agent now
  returns `unimplemented` if either RPC carries a `become_user`, and
  the corresponding `stat`/`file`/`copy` action overrides fall back
  to the builtin module (which goes through ansible's normal become
  path and runs as the target user) whenever `become_user` is set.

## 0.5.4 — April 20, 2026

### Bug fixes

- **Connection plugin: run the remote daemon as root in become mode.**
  0.5.2/0.5.3 launched the daemon as the target `become_user` (via
  `sudo -u <user>`). That was fine for sudoers but failed for any
  `become_user` that wasn't itself in `/etc/sudoers` (e.g. dedicated
  app users like `returns`): ansible's become layer still wraps
  every module with `sudo -u <user> /bin/sh -c …` and hands that
  wrapped command to the connection plugin, so the daemon — already
  running as `<user>` — executed `sudo` and hit
  `<user> is not in the sudoers file.` The daemon now runs as root
  (one instance per host), matching the invariant the `runuser`-
  dropping logic in `exec_command` was originally written against,
  and ansible's `sudo -u X` wrapper works because root can re-exec
  as any uid regardless of sudoers policy.

### Other changes

- Removed the 0.5.3 `system_agent_path` option. With the daemon
  running as root it can exec the binary from the connecting user's
  0700 home, so the `/usr/local/libexec/` staging dance is no longer
  needed. The one-path `agent_path` option (unchanged default
  `~/.ansible/fastagent/…`) covers every case.
- Remote socket path is now `/tmp/fastagent-root.sock` for all
  become tasks regardless of target `become_user`, replacing the
  per-user `/tmp/fastagent-<user>.sock` naming. Non-become tasks
  keep `/tmp/fastagent.sock`.

## 0.5.3 — April 20, 2026

### Bug fixes

- **Connection plugin: stage the agent under `/usr/local/libexec` for
  non-root `become_user`.** The default agent path
  (`~/.ansible/fastagent/`) lives inside the connecting user's home
  directory, which is typically mode 0700. That works for
  `become_user: root` (root bypasses DAC) but fails for any other
  `become_user`: sudo drops privileges before execve and the target
  uid cannot traverse `/home/<ssh_user>`, so the daemon never starts
  and the error surfaces as
  `fastagent: failed to start daemon: rc=1 / timeout waiting for
  socket`. The plugin now detects this case and stages the binary via
  `scp` to `/tmp/` followed by `sudo install -D -m 0755` into
  `/usr/local/libexec/fastagent/`, which is root-owned and
  world-traversable (0755), so any `become_user` can exec it. A new
  option `system_agent_path` (ansible var
  `fastagent_system_agent_path`) overrides this destination if a site
  needs something different. `become_user: root` and direct (non-
  become) execution keep the existing `~/.ansible/fastagent/` path.

## 0.5.2 — April 20, 2026

### Bug fixes

- **Connection plugin: kill the right daemon on bootstrap.** The kill
  step before starting a new daemon used `pkill -f 'fastagent --daemon'`,
  a literal pattern that never matched the real cmdline
  (`fastagent-X.Y.Z-OS-ARCH --daemon`, no space after `fastagent`).
  On a version upgrade the old daemon kept the socket bound, the new
  daemon failed to start, and the error surfaced as
  `fastagent: failed to start daemon: rc=1 / timeout waiting for
  socket`. The kill path now uses `pkill -F {socket}.pid` (integer-
  parsed from the daemon's own pid file, so a garbage or adversarial
  pid file can't be used to signal arbitrary PIDs) with a corrected
  backstop regex `fastagent[^ ]* --daemon`.

- **Connection plugin: detect stale SSH -L forwarders.** The fast-path
  `_try_local_socket` only checked that the local socket file existed
  and that `connect()` did not block. A stale forwarder pointing at a
  dead remote socket accepts the local connect, but the remote side's
  connect to the dead daemon returns ECONNREFUSED, so the first RPC
  read saw EOF and ansible reported
  `fastagent: no response (agent process may have exited)`. The plugin
  now sends a `Hello` inside the existing 2s probe timeout and only
  declares the fast path usable after a valid response; otherwise it
  closes the socket and falls through to bootstrap. Regression tests
  added in `plugins/connection/fastagent_test.py`.

## 0.5.1 — April 20, 2026

### Bug fixes

- **`file` action: infer `state=directory` for existing directories.** When
  a task omits `state` and the target path already exists as a directory,
  fastagent now matches `ansible.builtin.file`'s documented default and
  treats it as `state=directory`. Previously, fastagent defaulted to
  `state=file`, causing tasks that only set `mode`, `owner`, or `group` on
  an existing directory to fail. The inference logic was extracted into
  `plugins/module_utils/file_state.py` and covered by a new unit test suite
  in `tests/test_file_action.py`.

### Documentation

- **README: added one-shot install snippet for fastagent setup.**

## 0.5.0 — April 19, 2026

### New features

- **`pipelining` connection option.** The connection plugin now declares
  `has_pipelining = True` and a `pipelining` option matching SSH's
  surfaces (env `ANSIBLE_PIPELINING` / `ANSIBLE_SSH_PIPELINING` /
  `FASTAGENT_PIPELINING`, ini `[ssh_connection] pipelining`, vars
  `ansible_pipelining` / `ansible_ssh_pipelining`). With pipelining on,
  non-overridden modules skip the `put_file` round-trip and have their
  bytes piped via stdin — measured savings of ~10s/run on a converged
  plex deploy at home and ~25s/run from a higher-latency link.

- **Module shims for `copy`, `file`, `stat`, `command`.** Routing-only
  stubs added to `plugins/modules/`. They exist so the
  `collections: [kevinburke.fastagent]` play keyword resolves
  unqualified `copy:` / `file:` / `stat:` / `command:` to our collection
  and selects our action plugin override. Without these, the keyword
  only routed `apt` / `systemd` (the two existing module shims).

- **`FASTAGENT_TRACE=<file>` env var.** When set, every JSON-RPC call
  records `timestamp_ns\tmethod\tduration_ms\thint` to the named file
  (TSV). Useful for finding hot RPCs and overhead between RPCs without
  patching code.

### Bug fixes

- **`command:` action override sent unparsed cmd_string.** Tasks like
  `command: su - user -c 'cmd with spaces'` were forwarded to the
  agent as a single string and then split with `strings.Fields` on the
  Go side, which doesn't respect quotes — `su` received `--user` as a
  flag instead of as part of the quoted command body. The action
  plugin now sends `shlex.split(cmd_string)` as argv. The agent's
  `strings.Fields` cmd_string handling remains a footgun and is tracked
  in TODO.

- **Action plugin fallbacks used `ansible.legacy.X` instead of
  `ansible.builtin.X`.** Once a user wires up `library = .../fastagent/
  modules` in `ansible.cfg` (the documented setup that makes overrides
  fire fleet-wide), the shim modules sit in the `ansible.legacy`
  namespace and silently shadow the real builtins. Any
  `_execute_module(module_name="ansible.legacy.command", ...)` call
  then invoked the shim, which fails loudly. All fallback paths in
  `apt`, `command`, `file`, `stat`, `systemd` now use
  `ansible.builtin.X`.

- **`stdin_add_newline` mutated pipelined module bytes.** The agent's
  default of appending `\n` to stdin would corrupt module wrappers when
  pipelining is on. The connection plugin now passes
  `stdin_add_newline=False` so bytes go through verbatim.

### Documentation

- **README rewritten with measured numbers.** Updated the speedup
  claim to reflect what was actually measured on a converged plex
  deploy: 30s vs 57-69s baseline (~50% faster), not the previous
  "22s / 60% faster" memory. Added an honest framing about when the
  speedup grows (cold deploys, high-latency links, many small tasks)
  and where it bottoms out (Ansible's own per-task Python overhead).

- **Documented the routing-setup requirement.** Replaced the old
  "use unqualified module names" section with a concrete two-option
  setup story: legacy paths in `ansible.cfg` (recommended, one-line,
  fleet-wide) or per-play + per-role `collections:` keywords
  (collection-pure, more invasive). The previous version implicitly
  assumed the legacy resolution path that the collection migration
  had broken.

## 0.4.0 — April 16, 2026

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
