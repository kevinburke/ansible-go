# Ansible Compatibility Audit Plan

Reference baseline: compare fastagent against stock `ansible-core 2.20.4`
behavior, with older supported behavior checked where the public contract changed
since `meta/runtime.yml` says `requires_ansible: ">=2.12.0"`.

The goal is to stop fixing compatibility one report at a time. Every fast path
should either:

1. match the stock Ansible result and side effects for a documented subset, or
2. fall back to `ansible.builtin.*` before doing any work, or
3. document a known incompatibility with a test proving the current behavior.

The known `file: state=directory recurse=true owner=... group=...` tree walk
gap is intentionally excluded from this plan because it is being fixed
separately.

## Workflow

Each compatibility fix should be handled as one reviewable work item:

- create or reuse a dedicated worktree for the fix group
- keep the implementation scoped to one compatibility problem
- add a focused regression test before or with the behavior change
- update this document when the fix lands, moving the item out of known
  problems or tightening the remaining caveat
- run the narrowest useful tests and record them in the commit/final note
- create a commit in the worktree when the work is complete

Subagents should include this checklist in their task instructions. Their final
response should name the commit they created, list changed files, list tests
run, and call out any remaining compatibility gap.

## Known Compatibility Problems

These are visible from the current Go/Python fast paths before doing a deeper
source comparison.

### command/shell

- Non-shell `cmd_string` is rejected by the Exec RPC; fastagent's command
  action plugin must send `argv` after shlex parsing or fall back to
  `ansible.builtin.command`.
- `creates` and `removes` now use glob matching relative to `chdir` on the
  fast path. Tasks combining those guards with non-root `become_user` fall back
  to the builtin command module because the Go RPC cannot yet evaluate the
  guards in the become user's execution context.
- Non-shell tasks whose arguments need target-side `expand_argument_vars`
  behavior fall back to the builtin command module. The fast path still handles
  arguments without `$` or leading `~`, and tasks that explicitly set
  `expand_argument_vars=false`.
- The Go side only returns `rc`, `stdout`, `stderr`, `changed`, `skipped`, and
  `msg`; the action plugin synthesizes some fields, but direct RPC behavior is
  not fully result-compatible with `ansible.builtin.command`.
- Become support assumes passwordless `sudo`. Other become methods must fall
  back to Ansible's own become wrapping before the command fast path runs.

### stat/read_file

- `Stat` and `ReadFile` reject `BecomeUser`. The action plugins fall back for
  become tasks, but direct RPC callers cannot match stock module behavior.
- Checksum support now covers Ansible's stat algorithms (`md5`, `sha1`,
  `sha224`, `sha256`, `sha384`, and `sha512`). The action plugin falls back to
  `ansible.builtin.stat` for unsupported algorithms and for stock's default
  `get_mime`/`get_attributes` probes unless callers opt out.
- `stat` result coverage is intentionally close for the fast subset, but should
  still be diffed against stock output for special files, inaccessible paths,
  uid/gid lookup failures, and mount option effects.

### copy/template

- Directory copy falls back to the builtin action. That is probably correct,
  but the fallback is fragile because it loads ansible-core's copy action by
  file path and rewrites internal `ansible.legacy.*` calls.
- SELinux attributes and labels are not applied by `WriteFile`.
- `force=false` now avoids replacing an existing destination in the fast path.
- Diff generation now fails before writing if the fast path cannot read the old
  file for a requested diff.
- Backup file naming is simplified and may not match Ansible's timestamp and
  metadata behavior.

### file

- `mode` parsing only accepts octal strings/ints after the action plugin's
  simple normalization. Symbolic modes like `u=rw,g=r,o=` are not supported.
- `modification_time` and `access_time` are parsed in the action plugin but not
  sent to or applied by the Go handler.
- `follow` is not consistently honored by file attribute operations because
  Go uses `os.Stat`, `os.Chmod`, and `os.Chown`.
- Link and hardlink behavior is simplified: `force`, ownership, mode handling,
  relative links, existing directory/file edge cases, and atomic replacement
  should be compared against stock.
- `state=touch` always changes timestamps for existing files and does not
  implement Ansible's time formatting knobs.
- `state=absent` returns a minimal result and does not expose the same
  `path_contents`/diff behavior stock Ansible can produce.
- The file action currently sets `uid` and `gid` to `0` after a successful
  `state=file` operation instead of copying values from stat.

### apt/package/dnf

- The apt action fast path supports only simple package names via
  `name/pkg/package`, `state=present/absent/latest` plus the older
  `installed/removed` aliases, `update_cache`, and `cache_valid_time`.
  Explicit unsupported `ansible.builtin.apt` arguments fall back to
  `ansible.builtin.apt` before the Package RPC, including `purge`,
  `autoremove`, `allow_downgrade`, `only_upgrade`, `install_recommends`,
  `policy_rc_d`, `default_release`, non-default `dpkg_options`, lock timeout
  tuning, deb file installs, `build-dep`, `fixed`, `upgrade`, package version
  constraints, architecture-qualified package specs, paths, and wildcards.
- The apt module shim has no safe builtin fallback because it shadows
  `ansible.legacy.apt` for generic `package` dispatch, so it fails before
  package operations when those explicit unsupported arguments or package specs
  are supplied.
- `state=latest` still uses `apt-get install --yes --upgrade`, which needs
  reference comparison with `ansible.builtin.apt`.
- The changed detection from apt output is string-based and likely wrong for
  several no-op/update/remove cases.
- The dpkg installed cache treats package names literally. Version constraints,
  architecture suffixes, paths, and wildcard package specs now fall back/fail
  before using the cache; virtual packages still need reference tests because
  simple names cannot be distinguished syntactically.
- The dnf/yum shim supports simple package names with
  `state=present/absent/latest` plus `installed/removed` aliases. It accepts
  the stock ansible-core 2.20.4 dnf argument names for parsing, but any
  explicit unsupported value such as `update_cache`, repo/plugin selection,
  `autoremove`, `security`, `bugfix`, `download_only`, `allowerasing`, alternate
  roots, package specs, RPM paths, or `list` fails before running dnf/yum
  instead of being silently ignored.
- The Go `Package` RPC does not model check mode; action plugins synthesize
  rough check-mode results for package-manager shims. The apt action now falls
  back to `ansible.builtin.apt` for check mode.

### systemd/service

- The systemd fast path supports only `name`, `state`, `enabled`, and
  `daemon_reload`; it falls back for `masked`, non-system scopes, and
  `daemon_reexec`.
- `no_block` is parsed but ignored.
- `daemon_reload` always marks changed. Stock behavior should be checked.
- Service state detection ignores failed/inactive/activating nuances and
  ignores errors from `systemctl is-active` and `is-enabled`.
- `reloaded` always runs `systemctl reload`; stock behavior and failure shape
  for services that cannot reload should be compared.
- There is a TODO saying the systemd override may not fire in real playbooks;
  routing should be verified separately from semantic parity.

### connection/raw/module fallback

- Unsupported become methods fall back to Ansible's wrapping. That is the right
  safety choice, but needs regression tests so unsupported methods never run
  with silently elevated or lowered privileges.
- `_try_local_socket` can connect to a stale local forward and then fail on the
  first RPC if the remote daemon timed out. This is not a module semantic issue,
  but it creates user-visible differences from stock SSH reliability.
- Fallbacks must call `ansible.builtin.*`, not `ansible.legacy.*`, whenever the
  fastagent legacy module shims are on the module path.

## Reference Harness

Build a parity harness that can run the same task twice:

- stock: `ansible_connection=ssh` or `local`, with no fastagent action path
- fastagent: `ansible_connection=kevinburke.fastagent.fastagent`

For each case, capture:

- full JSON result after removing expected volatile fields such as timestamps,
  temp paths, `invocation`, elapsed deltas, and backup suffixes
- filesystem state after the task
- command/service/package side effects where relevant
- whether a fastagent task used the fast path or fell back

The harness should fail loudly when a fast path claims support but differs from
the reference. If a case is intentionally unsupported, assert that it falls back
to `ansible.builtin.*`.

## Subagent Work Plan

Each subagent should start by reading the stock Ansible source for
`ansible-core 2.20.4` installed locally, then write focused parity tests before
changing behavior.

### Agent 1: command/shell parity

Owned files: `exec.go`, `plugins/action/command.py`, command tests.

Tasks:

- Build a matrix for `cmd`, free-form args, `argv`, shell, quoted arguments,
  `chdir`, `creates`, `removes`, stdin, newline stripping, check mode,
  environment, timeout, nonzero rc, and become.
- Prove every unsupported become/privilege case falls back or fails loudly.
- Decide whether `handleExec` should reject unparsed `cmd_string` without
  `use_shell` or use a real shell lexer.

### Agent 2: stat/read_file/copy parity

Owned files: `fileops.go`, `plugins/action/stat.py`,
`plugins/action/copy.py`, copy/stat tests.

Tasks:

- Compare stat output for file types, symlinks with `follow` true/false,
  checksum algorithms, missing paths, unreadable paths, uid/gid lookup
  failures, and special files.
- Add fallback tests for unsupported checksum algorithms and become.
- Check copy semantics for `content`, `src`, destination directories,
  `force=false`, `backup`, `mode`, `owner`, `group`, `validate`, diff mode,
  check mode, vault-decrypted files, and SELinux args.

### Agent 3: file parity

Owned files: `fileops.go`, `plugins/action/file.py`,
`plugins/module_utils/file_state.py`, file tests.

Tasks:

- Compare `state=file,directory,touch,absent,link,hard` across existing,
  missing, symlink, and wrong-type paths.
- Exclude the known recursive directory attribute walk issue from this branch.
- Cover symbolic modes, `follow`, `force`, access/modification time options,
  link replacement, hardlink errors, check mode, diff mode, and result fields.
- Fix the action plugin's incorrect `uid`/`gid` result fields.

### Agent 4: apt/dnf/package parity

Owned files: `package.go`, `plugins/action/apt.py`,
`plugins/modules/apt.py`, `plugins/modules/dnf.py`, package tests.

Tasks:

- List every accepted Ansible apt/dnf argument and classify it as implemented,
  fallback, or unsupported.
- Add preflight fallback for any argument currently parsed but ignored,
  especially `purge`.
- Reference-test package specs with versions, architecture suffixes, virtual
  packages, multiple packages, no-op installs, removes, `latest`,
  `update_cache`, and check mode.
- Replace fragile changed detection where it differs from stock output.

### Agent 5: systemd/service and routing parity

Owned files: `service.go`, `plugins/action/systemd.py`,
`plugins/modules/systemd.py`, connection/routing tests.

Tasks:

- Verify whether the systemd action override fires for unqualified,
  `ansible.legacy`, collection-routed, and builtin-pinned tasks.
- Compare `started`, `stopped`, `restarted`, `reloaded`, `enabled`,
  `daemon_reload`, `no_block`, missing services, failed services, and check
  mode.
- Add fallback tests for `masked`, `scope`, `daemon_reexec`, and unsupported
  service managers.
- Decide whether `no_block` should be implemented or should trigger fallback.

### Agent 6: compatibility docs and guardrails

Owned files: `README.md`, `docs/testing.md`, `TODO`, lint/tests for plugin
fallbacks.

Tasks:

- Turn the audited matrix into user-facing compatibility docs.
- Add a test or lint that rejects `ansible.legacy.*` fallback calls from
  `plugins/action/*.py`.
- Add a generated-artifact check so docs and any compatibility fixture outputs
  do not drift.
- Document exact Ansible versions used for compatibility testing.

## Exit Criteria

- Every accelerated module has a checked-in compatibility matrix.
- Every matrix row is implemented, falls back before side effects, or is listed
  as a known incompatibility.
- The known incompatibility list is user-visible and versioned.
- CI runs the parity tests that do not require privileged package/service
  changes, and the privileged cases have a documented manual or containerized
  runner.

## Fixed Items

- `copy validate:` falls back to ansible-core's builtin copy action before the
  fast path. `WriteFileParams.Validate` is still not implemented in Go, but the
  action plugin no longer silently skips validation for ordinary copy tasks.
