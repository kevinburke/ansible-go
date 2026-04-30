# Compatibility

Fastagent is an accelerator, not a replacement Ansible implementation. Each
accelerated action plugin should either match the corresponding
`ansible.builtin.*` behavior for the documented subset, fall back to the
builtin action before making changes, or document the known gap here.

Compatibility is audited against `ansible-core 2.20.4`. The collection declares
`requires_ansible: ">=2.12.0"` in `meta/runtime.yml`; older supported versions
are checked when Ansible changed a public behavior that fastagent relies on.

## Summary

| Module | Fast path | Falls back for | Known gaps |
|--------|-----------|----------------|------------|
| `command`, `shell` | Exec RPC for argv/free-form commands, shell commands, chdir, stdin, creates/removes, and supported environment/become inputs. | Non-fastagent connections and become methods handled by Ansible's normal module wrapping. | Direct RPC callers that send only `cmd_string` with `use_shell=false` still hit Go's `strings.Fields` splitting. `creates` and `removes` are checked as the daemon uid. Direct RPC results are narrower than `ansible.builtin.command`. Passwordless sudo is the only fast-path become model. |
| `stat` | Stat RPC for default stat output and SHA-256 checksums. | Become tasks and checksum algorithms other than SHA-256. | Direct RPC callers cannot use become. Output still needs parity checks for symlinks, special files, inaccessible paths, uid/gid lookup failures, and mount option effects. |
| `copy`, `template` | WriteFile RPC for common file copy/content/template writes with checksum, mode, owner/group, backup, diff, and check-mode handling in the action plugin. | Directory copy, `validate`, non-fastagent connections, and builtin copy fallback paths that need ansible-core semantics. | SELinux labels are not applied. `force=false`, backup naming, and diff read-error behavior still need reference checks. |
| `file` | File/Stat RPC for common `state=file`, `directory`, `touch`, `absent`, `link`, and `hard` paths. | Non-fastagent connections and unsupported action-plugin preflight cases. | Symbolic modes are not supported. Access/modification time options are parsed but not applied by Go. `follow`, link replacement, hardlink edge cases, `touch`, `absent` diff fields, and some result fields still differ from stock Ansible. Recursive directory ownership/group walks are tracked separately. |
| `apt`, `package`, `dnf` | Package RPC for a small apt subset: `name`/`pkg`/`package`, `state`, `update_cache`, and `cache_valid_time`; dpkg status cache avoids no-op installs. | Non-fastagent connections and action-plugin unsupported cases. | Many apt/dnf arguments are accepted by Ansible but not implemented by the fast path, including purge/autoremove/downgrade/recommends/default release/dpkg options/lock timeout/deb installs. `purge` is parsed but ignored. `latest`, changed detection, package specs, virtual packages, architecture suffixes, and check mode need parity tests. |
| `systemd`, `service` | Service RPC for `name`, `state`, `enabled`, and `daemon_reload`. | `masked`, non-system scopes, `daemon_reexec`, unsupported service managers, and non-fastagent connections. | `no_block` is parsed but ignored. `daemon_reload` always reports changed. State detection is simplified, reload behavior needs stock comparison, and routing still needs coverage for all module name forms. |

## Routing and Fallback Contract

Fastagent's recommended routing uses only the collection action plugin path:

```ini
[defaults]
action_plugins = ~/.ansible/collections/ansible_collections/kevinburke/fastagent/plugins/action
```

Do not put `plugins/modules/` on Ansible's `library` path. Those files are
direct-dispatch refusal shims, not normal module implementations.

Every fallback inside `plugins/action/*.py` must explicitly call
`ansible.builtin.*`, not `ansible.legacy.*`. The action plugins are commonly
installed into Ansible's legacy action path so unqualified tasks can be
accelerated. If a fallback calls `ansible.legacy.*`, it can resolve back to
fastagent's shim or action plugin instead of core Ansible.

`tests/test_action_plugin_fallbacks.py` enforces this by rejecting executable
`ansible.legacy.*` module/action names in action plugin call sites.

## Known Incompatibilities

These are the user-visible gaps to account for during rollout:

- Registered result dictionaries from fast paths are intentionally smaller than
  stock module results in several modules. Playbooks that inspect uncommon
  result fields should be canaried first.
- SELinux file contexts are not set by the copy fast path.
- Non-sudo become methods rely on Ansible fallback paths rather than fastagent
  RPC support.
- Package and service fast paths cover common Linux cases only. Use
  `ansible.builtin.*` task names or `ansible_connection: ssh` for hosts or
  tasks that require full package/service-manager semantics.
- Hard-pinned `ansible.builtin.*` task names bypass fastagent action overrides.
  They still run through the connection plugin, but not through the Go RPC
  module fast path.

## Compatibility Test Versions

The current audited baseline is:

| Tool | Version |
|------|---------|
| Ansible | `ansible-core 2.20.4` |
| Controller Python used by Ansible | `Python 3.14.3` |
| Jinja | `3.1.6` |
| PyYAML | `6.0.3` with libyaml `0.2.5` |

The repository also runs Python unit tests with the default `python3` available
in the checkout environment; that may be a different interpreter from the one
Ansible itself uses.
