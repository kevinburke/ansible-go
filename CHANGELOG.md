# Changelog

All notable changes to fastagent are documented in this file.

## 0.7.3 — May 8, 2026

### Bug fixes

- **Accept native scalar values in `command: argv:`.** When
  `jinja2_native_types` rendered a single templated argv element as
  an integer, boolean, float, or null value, stock
  `ansible.builtin.command` stringified the value before exec but
  fastagent rejected the Exec RPC with a Go JSON unmarshal error.
  The agent now coerces scalar argv values to strings before
  execution, while arrays and objects fail with an indexed
  `argv[N]` error.

- **Open become-daemon logs as root.** When a play used
  `become: true`, the connection plugin ran the daemon under `sudo`
  but left `</dev/null >>/tmp/fastagent-root-<version>.sock.log
  2>&1` on the outer SSH user's shell. If a previous run had created
  that log as `root`, daemon startup failed before `sudo` executed
  with `Permission denied` and then `timeout waiting for socket`.
  The become daemon launch now moves the redirection inside
  `sudo sh -c`, so stale root-owned logs no longer block startup.

- **Tighten command and shell fast-path compatibility.** Non-shell
  Exec requests with only `cmd_string` now fail loudly instead of
  splitting with `strings.Fields`, `creates`/`removes` now use glob
  matching relative to `chdir`, and tasks with unsupported command
  features such as target-side argument expansion, executable
  overrides, unsupported become wrappers, or non-root
  `creates`/`removes` fall back to `ansible.builtin.command`.

- **Guard unsupported package-module options.** The `apt` and `dnf`
  fast paths now model a narrower, explicit subset of
  ansible-core's argument specs. Unsupported action-plugin options
  fall back to `ansible.builtin.apt` before the Package RPC, while
  module-shim-only routes fail before running `apt-get`, `dnf`, or
  `yum` with silently ignored arguments. `cache_valid_time` also
  implies `update_cache`, matching stock `apt`.

- **Improve `stat`, `copy`, and `systemd` parity.** `stat` now
  supports requested checksum algorithms and falls back for stock
  mime, attribute, and SELinux probes. `copy` avoids fast-path side
  effects for SELinux and preserve-mode cases, honors
  `decrypt=false` and `force=false`, rejects content-to-directory
  writes before changing the target, and surfaces diff read errors.
  `systemd` now carries `no_block` through the RPC, treats
  daemon-reload-only tasks as unchanged, and routes unsupported
  masked, scoped, and daemon-reexec cases away from the fast path.

### Documentation

- **Add the compatibility audit plan and fix workflow.** The repo now
  records how fastagent checks its module overrides against
  ansible-core behavior and classifies each option as implemented,
  fallback, or explicit failure.

## 0.7.2 — April 30, 2026

### Bug fixes

- **Honor `recurse: true` for `ansible.builtin.file` directory
  tasks.** The File RPC parsed `recurse` but ignored it when
  `state=directory`, so tasks like `file: path=/srv/git
  state=directory owner=git group=git recurse=true` updated only the
  leaf directory and left existing descendants untouched. The daemon
  now walks descendants after ensuring the directory exists, applies
  owner/group/mode only when they differ, and preserves the
  `follow:` symlink behavior sent by the action plugin.

## 0.7.1 — April 28, 2026

### Bug fixes

- **Invalidate the apt cache when `/etc/apt/sources.list.d/` changes.**
  The `apt` action's "skip apt-get update if cache is fresh"
  optimization tracked only how recently the cache had been refreshed
  and ignored modifications to the sources directory. A play that
  wrote a new `.sources` file (typically via `deb822_repository`,
  which runs in upstream Python and is invisible to fastagent) and
  then ran `apt: update_cache=true` within the freshness window would
  silently skip the update, then fail to install packages from the
  newly added repo with `Unable to locate package`. The skip now
  also requires that the recorded update time is newer than the
  latest mtime under `/etc/apt/sources.list` and one level of
  `/etc/apt/sources.list.d/`; if any source is newer, fastagent falls
  through to `apt-get update` and invalidates the dpkg cache as
  before. Both the in-memory and on-disk (`/var/lib/apt/lists/lock`
  mtime) skip paths share the same predicate.

## 0.7.0 — April 28, 2026

### Bug fixes

- **Return the full `ansible.builtin.stat` field set from the `Stat`
  RPC.** Playbooks that consumed synthesized fields like
  `q.stat.executable`, `q.stat.readable`, `q.stat.uid`,
  `q.stat.nlink`, or any of the per-bit booleans
  (`xusr`/`wusr`/`rusr`/...) crashed mid-conditional under fastagent
  with `object of type 'dict' has no attribute 'executable'` rather
  than evaluating to false: the Go agent populated only a fraction of
  the keys ansible.builtin.stat would have set, so `assert: { that:
  q.stat.executable }` raised instead of skipping. The Stat RPC now
  returns `uid`, `gid`, `inode`, `dev`, `nlink`, `ctime`, the type
  flags (`isreg`/`isblk`/`ischr`/`isfifo`/`issock`), the suid/sgid
  bits, every per-bit permission boolean, and the three
  `access(2)`-derived booleans `readable`/`writeable`/`executable`
  (queried from the kernel so `noexec` and `ro` mount flags are
  honored, matching `os.access` in stock stat). The action plugin
  copies these through, defaults missing keys to `False`/`0`/`""` so
  playbooks see the key rather than a missing attribute, and
  populates `pw_name`/`gr_name` only when the uid/gid resolved.

- **Symlink fields now follow the `ansible.builtin.stat` convention.**
  When the path is a symlink, `stat.lnk_target` is the raw
  `os.readlink` result (possibly relative) and `stat.lnk_source` is
  the resolved real path (`filepath.EvalSymlinks` /
  `os.path.realpath`). Previously the agent sent the readlink output
  as `lnk_source` and shipped no `lnk_target` at all; the action
  plugin masked the divergence by aliasing `lnk_target` to
  `lnk_source`, so playbooks that compared `lnk_target` against the
  raw symlink string saw the resolved absolute path instead.

### Internal

- **Switch the agent from `syscall` to `golang.org/x/sys/unix`.** The
  Stat RPC now reads raw `Stat_t` fields directly via `unix.Lstat` /
  `unix.Stat` instead of going through `os.Lstat` and an
  `info.Sys().(*syscall.Stat_t)` assertion, and `applyOwnershipAndMode`
  reads ownership in one syscall instead of two. No user-visible
  protocol change.

## 0.6.7 — April 26, 2026

### Bug fixes

- **Pick a traversable cwd for `become_user` execs.** Tasks that ran
  with `become_user:` and no `chdir:` inherited the daemon's own cwd
  into the child process. If the daemon was started in a directory
  the become user couldn't traverse — typically another user's
  `$HOME` at mode `0700` — any subprocess that fork/execed or walked
  the filesystem failed with `permission denied`, even though the
  binary itself was readable. Symptoms included `go install` failing
  inside the toolchain (`fork/exec /usr/local/go/pkg/tool/.../compile:
  permission denied`) and `git`/`gh` operations that walked toward
  `.git` from the inherited cwd. fastagent now defaults `cmd.Dir` to
  the target user's home directory, falling back to `/`, whenever
  `become_user` is set with no explicit `chdir`.

- **Propagate the task `environment:` block through the command/shell
  override.** The fastagent `command` action plugin called the Exec
  RPC without `env=`, so any environment declared on the task was
  silently dropped on the floor. Tasks that depended on
  `environment:` for variables like `GO111MODULE`, `GOBIN`, or
  `PATH` ran with none of them set, often surfacing as a follow-on
  error from the program rather than a missing-env error. The action
  plugin now templates and merges `self._task.environment` into a
  dict and passes it through the RPC, mirroring
  `ActionBase._compute_environment_string`.

## 0.6.6 — April 23, 2026

### Bug fixes

- **Accept numeric UID/GID strings for `owner` and `group`.** Tasks like
  `file: path=/var/data state=directory owner='1001' group='1001'` failed
  with `lookup user "1001": user: unknown user 1001` whenever the UID had
  no `/etc/passwd` entry (typical for podman/container UID ranges or
  pre-creating ownership for a service user that hasn't been added yet).
  ansible-core's file module treats numeric strings as literal IDs and
  passes them straight to `chown(2)`; fastagent now does the same.

## 0.6.5 — April 24, 2026

### Documentation

- **Stop recommending `library = .../fastagent/plugins/modules`.** The
  README's "Recommended: legacy paths in `ansible.cfg`" section and its
  one-shot install snippet both told users to set
  `library = …/fastagent/plugins/modules` alongside
  `action_plugins = …/fastagent/plugins/action`. Only the
  `action_plugins` entry is needed to shadow unqualified `copy:`,
  `stat:`, `file:`, `apt:`, `command:`, and `systemd:` with the
  fastagent overrides.

  Adding `library` to the legacy search path puts fastagent's module
  shims — whose only job is to refuse direct invocation — in front of
  every ansible-core action plugin's internal
  `_execute_module(module_name="ansible.legacy.<stat|copy|file>", …)`
  call. Core action plugins like `unarchive` (calls
  `_execute_remote_stat` on `dest/` before extracting), `template`
  (dispatches through `ansible.legacy.copy` after rendering),
  `get_url`, `archive`, and `patch` all hit the shim and die with
  "kevinburke.fastagent.<name> shim was invoked directly".

  0.6.4's `_shield_builtin_from_legacy_shims` wrapped
  `_execute_module` on the one builtin-copy instance fastagent's copy
  fallback creates, but that shield only covers that specific instance
  — every other core action plugin runs outside fastagent's call
  stack, so the only reliable fix is to keep the shims off the legacy
  module search path in the first place.

  The README now documents "do NOT also set `library = …`" inline and
  the one-shot install snippet emits `action_plugins` only.

## 0.6.4 — April 23, 2026

### Bug fixes

- **Break fallback recursion when fastagent is on the legacy
  `action_plugins` search path.** 0.6.3's copy fallback resolved the
  builtin via `action_loader.get("ansible.legacy.copy")`. That works in
  stock ansible, but any caller that puts the fastagent plugins on the
  legacy path to shadow unqualified `copy:` (the recommended
  caracal-server `ansible.cfg` layout) causes `ansible.legacy.copy` to
  resolve *back into this override*. The fallback then re-entered our
  `run()`, hit the same branch, and recursed until CPython raised
  `RecursionError: maximum recursion depth exceeded`. The visible
  failure in ansible was "Unhandled exception when retrieving
  'DISPLAY_TRACEBACK': maximum recursion depth exceeded" on any task
  the fast path punts (directory `src:`, `remote_src: true`, etc.).

  Fixed by importing `ansible.plugins.action.copy.ActionModule` by
  module path and instantiating it directly. Neither legacy-path
  shadowing nor any future loader override can alias the fallback
  target back to this class.

  Added a test that simulates the shadowed configuration
  (`action_loader.get` returns this ActionModule) and asserts the
  fallback still completes in one call.

- **Shield the builtin copy action from our own legacy shims.** Once
  the copy fallback instantiated ansible-core's builtin copy action
  directly, a second failure surfaced under the same legacy-path
  shadowing: the builtin internally calls `_execute_module(
  module_name="ansible.legacy.stat")`, `ansible.legacy.copy`, and
  `ansible.legacy.file` (via `_execute_remote_stat` and the main
  transfer paths). Those names resolve through the legacy module
  library search path, which caracal-server points at fastagent's
  module shims — files whose only job is to refuse direct invocation
  with "kevinburke.fastagent.X shim was invoked directly". The
  fallback therefore aborted mid-copy on the first stat call.

  Fixed by wrapping `_execute_module` on the fallback instance so any
  `ansible.legacy.<name>` call is rewritten to `ansible.builtin.<name>`
  before dispatch. The rewrite is scoped to the single builtin-copy
  instance the fallback creates, so global module resolution is
  untouched. Added a regression test that simulates the three internal
  legacy calls and asserts they all reach `ansible.builtin.*`.

## 0.6.3 — April 23, 2026

### Bug fixes

- **Fall back to the copy action plugin, not the copy module.** The
  fastagent copy override's `_run_builtin_copy` helper called
  `_execute_module("ansible.builtin.copy")`, which ships the copy
  *module* to the remote and runs it there. The module expects `src:`
  to be readable on the remote host — the *action plugin* is what
  walks local directories, uploads each file into a remote tempdir,
  and only then invokes the module with a remote path. Every case the
  fast path punted back to the builtin (directory `src:`,
  `_find_needle` misses, vault `get_real_file` failures, non-fastagent
  transport, and become) therefore failed with `Source <controller
  path> not found`. The simplest reproducer is a `copy: src=somedir/
  dest=...` with a directory source.

  Fixed by resolving `ansible.legacy.copy` through the action loader
  and calling its `run()`. `ansible.legacy.copy` maps to the builtin
  copy action; our override sits under `kevinburke.fastagent.copy`, so
  the legacy name can't loop back into us.

  Also aligned the Makefile `test` target with CI so
  `tests/test_copy_action.py` is actually executed — it was added in
  0.6.1 for the vault-decrypt fix but was never listed in the
  Makefile's named-module invocation, and the new directory-fallback
  regression test would have been skipped the same way.

## 0.6.2 — April 23, 2026

### Bug fixes

- **Chown `WriteFile` intermediate directories to the target user, not
  the daemon uid.** When a play used `become: true`, the fastagent
  daemon ran as root on the remote host. Its `WriteFile` RPC handler
  called `os.MkdirAll(dir, 0o755)` to create any missing parents of
  the destination and only applied the caller's `owner`/`group` to
  the final file. Intermediate directories created under the ssh
  user's home were therefore left `root:root 0o755`. That was latent
  until a later ansible run used stock ssh (or fastagent without
  become): the first task's `mkdir ~/.ansible/tmp/ansible-tmp-<ts>`
  then failed with `Permission denied` because the ssh user no longer
  had write access to its own `~/.ansible/tmp/`.

  Fixed by creating each missing intermediate segment individually and
  applying the `WriteFile` `owner`/`group` parameters (set to
  `remote_user` by the connection plugin's `put_file`) to each new
  segment. Pre-existing ancestors are left untouched, matching
  ansible's file module. If a host is already broken from an earlier
  release, fix it with `sudo chown -R <user>:<user> ~<user>/.ansible/`
  before re-running.

## 0.6.1 — April 22, 2026

### Bug fixes

- **Decrypt vault-encrypted source files in the copy fast path.** The
  fastagent copy action override read `src:` files directly with
  `open(source, "rb")`, bypassing ansible's normal vault handling. A
  task like `copy: src=garage-control/RATGDO_KEY dest=...` whose source
  was an `$ANSIBLE_VAULT;1.1;AES256` file therefore landed raw vault
  ciphertext on the remote host instead of the decrypted secret. Any
  consumer of the file (systemd `EnvironmentFile=`, an app reading the
  token, etc.) saw the ciphertext and malfunctioned; the secret was also
  left in plaintext ciphertext-form on disk, which is not what the
  playbook author asked for.

  Fixed by routing the source path through
  `self._loader.get_real_file(source, decrypt=True)`, matching how
  ansible's builtin copy action resolves the source. Unencrypted files
  are unaffected (the loader returns their original path); encrypted
  files are decrypted into a managed temp file before we read. If any
  host was deployed with 0.6.0 or earlier, check any file produced by a
  `copy:` task whose source was a vault file, and re-run the playbook
  with 0.6.1 to overwrite it.

## 0.6.0 — April 22, 2026

### Bug fixes

- **Apply task attrs to newly-created intermediate directories,
  matching ansible.** A `file:` task that created a deep directory
  tree with `owner`, `group`, or a restrictive `mode` only applied
  those attributes to the leaf directory. Any missing parents were
  still created by `os.MkdirAll` as `0755 root:root`, which diverged
  from ansible's `ensure_directory()` behavior and could break
  workloads that needed to traverse those intermediates as the target
  user. One concrete failure mode was rootless podman bind mounts
  under a freshly-created app-owned tree returning `statfs ...
  permission denied` because the parent directories stayed root-owned.

  Fixed by replacing the `MkdirAll` path with an
  ansible-matching directory walk that creates each missing segment
  individually and applies the task's owner/group/mode to every new
  intermediate. Existing ancestors are left untouched, again matching
  ansible's semantics. Regression coverage now checks both sides of
  that behavior: new intermediates receive the requested attributes,
  while pre-existing ancestors keep their current permissions.

## 0.5.9 — April 21, 2026

### Bug fixes

- **Route the command/copy/file/stat action overrides through a
  shared become-user helper.** 0.5.8 fixed `exec_command` to read
  `become_user` from `self._play_context` rather than the swallowed
  plugin, but the four action overrides still read
  `getattr(self._connection, "_become_user", None)` — an attribute
  0.5.8 deleted. `getattr` silently fell back to `None`, so command
  tasks with `become_user: "{{ app_user }}"` still hit the agent's
  Exec RPC with `become_user=None` and ran as root, tripping git's
  `dubious ownership` check on app-user-owned repos all over again.
  Same symptom as 0.5.7/0.5.8, different (and older) code path.

  Fixed by introducing `Connection.get_become_user()` as the single
  source of truth for "which user should the agent run this task
  as?". `exec_command` and all four action overrides now call it,
  and the copy/file/stat fallback guards (skip the fast path when
  becoming a non-root user) go through the same helper so they
  can't silently drift out of sync again.

## 0.5.8 — April 21, 2026

### Bug fixes

- **Read `become_user` from play_context, not the swallowed plugin.**
  0.5.7's `set_become_plugin` override captured the target user via
  `plugin.get_option("become_user")` and stored it on the connection.
  But ansible-core only populates those plugin options via
  `_set_plugin_options('become', ...)` when `connection.become is not
  None` (`task_executor.py:1085`) — and leaving `connection.become =
  None` is exactly what suppresses the `ActionBase` wrap that 0.5.7
  set out to avoid. So `get_option("become_user")` always returned
  the sudo plugin's default (`"root"`), which our code then mapped
  to "no wrap needed." Every become task ran as root.

  That was visible as git's `detected dubious ownership in repository
  at /opt/returns/data` when a root-run `git -C …` touched a
  `returns`-owned repo — the same CVE-2022-24765 protection that
  originally forced the 0.5.5 become rework.

  Fixed by reading `become_user` from `self._play_context.become_user`
  at `exec_command` time instead. Ansible templates that value from
  the task (`play_context.py:189`) before handing the play_context to
  the connection, so it reflects the task's actual `become_user`
  regardless of what the swallowed plugin's options look like.

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
