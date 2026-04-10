This design aims to accelerate Ansible execution without requiring users to
change how they write playbooks. The core idea is to keep standard Ansible
YAML, inventory, variables, templating, and controller-side planning exactly
as they are today, while replacing selected parts of the remote execution path
with a faster backend built around a persistent Go executor. The system should
transparently fast-path common tasks when possible, and fall back to stock
Ansible behavior whenever a task is unsupported or preserving semantics would be
risky.

Concretely, the implementation uses supported Ansible extension points below the
playbook language rather than changing the language itself. A custom connection
plugin provides a persistent transport and execution substrate, while selective
action-plugin overrides and module shims preserve Ansible’s existing task
semantics for common operations like template, copy, command, package, and
service. The result should be an accelerator, not a replacement frontend: users
keep writing ordinary Ansible, while the implementation opportunistically
substitutes a faster execution engine underneath.

**B = a custom connection plugin as the substrate**, and then
**A = selective semantic overrides on top**, split between **action overrides** for controller-side behavior and **module shims** for target-side behavior.

That fits Ansible’s current execution model: `TaskExecutor` hands module tasks to the normal action plugin, the normal action plugin loads the connection plugin, and action plugins are the supported place for controller-side preprocessing. Third-party strategy plugins are the part Ansible has deprecated for future removal, while custom action plugins and connection plugins are still documented extension points. Plugins also run on the control node and must be Python. ([Ansible Documentation][1])

The most useful translation for an implementer is this rule:

**Use a module shim whenever core already dispatches to `ansible.legacy.<name>` for you.
Use an action override when the semantics are fundamentally controller-side.**

That gives you a concrete build plan without requiring users to rewrite playbooks. The `ansible.legacy` detail matters because Ansible documents that `ansible.builtin` is pinned to core, while `ansible.legacy` can pick up custom plugins and modules from configured paths and adjacent directories. ([Ansible Documentation][2])

## What to put in the repo

For a prototype, I would keep it repo-local instead of starting with a collection:

```text
repo/
  ansible.cfg
  inventory/
  connection_plugins/
    fastagent.py
  action_plugins/
    copy.py
  library/
    command        # shim or binary module
    apt            # shim or binary module
    dnf            # shim or binary module
    systemd        # shim or binary module
    file           # optional shim
  module_utils/
    fastagent_client.py
```

Ansible already supports custom action plugins, connection plugins, modules, and shared `module_utils` from configured paths. The relevant knobs are `action_plugins`, `connection_plugins`, `library`, and `module_utils`; action plugins can also live adjacent to the play or inside a role. ([Ansible Documentation][3])

A minimal `ansible.cfg` for the prototype looks like:

```ini
[defaults]
connection_plugins = ./connection_plugins
action_plugins = ./action_plugins
library = ./library
module_utils = ./module_utils
```

## B: the custom connection plugin

This is the hook that exists regardless of how users wrote the task text. Connection plugins are selected per host with config, CLI, play keyword, or inventory variable; Ansible’s own docs use `ansible_connection` for that. The connection base class exposes the contract you need: `_connect()`, `exec_command()`, `put_file()`, `fetch_file()`, plus flags like `has_pipelining` and `supports_persistence`. ([Ansible Documentation][4])

A usable skeleton is:

```python
from ansible.plugins.connection import ConnectionBase
from ansible.errors import AnsibleConnectionFailure

class Connection(ConnectionBase):
    transport = "fastagent"
    has_pipelining = False      # only set True if you truly support pipelining semantics
    supports_persistence = True

    def _connect(self):
        if self._connected:
            return self
        self._client = FastAgentClient(
            host=self._play_context.remote_addr,
            user=self._play_context.remote_user,
        )
        self._client.connect()
        self._connected = True
        return self

    def exec_command(self, cmd, in_data=None, sudoable=True):
        r = self._client.exec_shell(cmd, stdin=in_data, sudoable=sudoable)
        return r.rc, r.stdout, r.stderr

    def put_file(self, in_path, out_path):
        with open(in_path, "rb") as f:
            self._client.put_file(out_path, f.read())

    def fetch_file(self, in_path, out_path):
        data = self._client.fetch_file(in_path)
  [118;1:3u      with open(out_path, "wb") as f:
            f.write(data)

    def close(self):
        if getattr(self, "_client", None):
            self._client.close()
        self._connected = False
```

For deployment, I would start with an **SSH-bootstrapped stdio agent** rather than a network daemon. That is an inference, not an Ansible requirement: it preserves your existing inventory auth story and avoids inventing a second trust fabric on day one. Later, if you want lower latency, you can switch the same plugin to a resident daemon.

The important point is that **B alone already works for anything that goes through normal module execution**, and it is the cleanest way to accelerate `raw`, because `raw` explicitly bypasses the module subsystem and runs directly through the configured remote shell. ([Ansible Documentation][5])

## A, translated into implementation choices

### 1) `command` and `shell`: use a **module shim**, not an action override

Current core source makes this easier than it looks. The builtin `command` action explicitly calls `ansible.legacy.command`, and the builtin `shell` action sets `_uses_shell=True` and then loads `ansible.legacy.command`. That means you can shadow `command` in `library/command` and catch both `ansible.builtin.command` and `ansible.builtin.shell` without changing task text. ([GitHub][6])

That shim can be either:

* a small Python module that RPCs to your agent, or
* a Go binary module, since Ansible supports binary modules and passes them through unchanged as long as they speak the normal JSON-in/JSON-out module contract. ([Ansible Documentation][1])

The shim should implement the documented semantics for:

* `creates` / `removes` for partial check mode,
* `stdin`, `stdin_add_newline`,
* `chdir`,
* `_uses_shell`,
* `strip_empty_ends`,
* and `expand_argument_vars` for `command`. ([Ansible Documentation][7])

A minimal Python shim shape is:

```python
from ansible.module_utils.basic import AnsibleModule

def main():
    module = AnsibleModule(
        argument_spec=dict(
            _uses_shell=dict(type='bool', default=False),
            cmd=dict(type='str'),
            argv=dict(type='list', elements='str'),
            chdir=dict(type='path'),
            creates=dict(type='path'),
            removes=dict(type='path'),
            stdin=dict(type='str', no_log=True),
            stdin_add_newline=dict(type='bool', default=True),
            strip_empty_ends=dict(type='bool', default=True),
            expand_argument_vars=dict(type='bool', default=True),
        ),
        supports_check_mode=True,
    )

    # implement creates/removes short-circuit first
    # then Exec RPC to the local agent socket
    module.exit_json(changed=True, rc=0, stdout="", stderr="")
```

This is one of the best places for Go, because the builtin action layer is already thin and the module boundary is stable.

### 2) `template`: use an **action override**, because the semantics are controller-side

Ansible documents that action plugins always run on the control node, and its program-flow guide uses `template` as the canonical example: the template action renders locally, then transfers a temp file and invokes `copy` remotely. The current `template` action source confirms that it generates template vars, renders with the templar, writes a local temp file, and then explicitly loads `ansible.legacy.copy`. ([Ansible Documentation][1])

That means the practical fast path is:

* do **not** teach the remote agent to parse Jinja;
* let the controller keep doing the render;
* override `copy` in `action_plugins/copy.py`;
* and have that override detect “rendered local file to remote path” and call `WriteFileIfChanged` on the agent.

A sketch:

```python
from ansible.plugins.action.copy import ActionModule as BuiltinCopyAction

class ActionModule(BuiltinCopyAction):
    def run(self, tmp=None, task_vars=None):
        if self._connection.transport != "fastagent":
            return super().run(tmp=tmp, task_vars=task_vars)

        args = self._task.args.copy()
        if args.get("remote_src") or args.get("content") is not None:
            return super().run(tmp=tmp, task_vars=task_vars)

        # supported local-file fast path:
        # 1. read local src
        # 2. checksum
        # 3. remote stat RPC
        # 4. validate/check_mode/diff handling
        # 5. atomic write RPC
        return result
```

The key fallback trick is: **import the builtin class directly** for fallback, instead of resolving `copy` through the loader again, or you will recurse into your own override.

### 3) `copy`: hardest case for true zero-rewrite

This is the main sharp edge.

If a playbook says `copy:` or `ansible.legacy.copy:`, your local `action_plugins/copy.py` override can replace it. But if the playbook hard-pins `ansible.builtin.copy`, Ansible’s documented `ansible.legacy` override behavior does not apply; `ansible.builtin` stays pinned to core. ([Ansible Documentation][2])

The builtin `copy` action still gives you some leverage, because current source shows it does remote stat/checksum work, transfers a temp file, and then calls `ansible.legacy.copy` and `ansible.legacy.file` internally. So a local `file` or `copy` module shim can reduce some of the later target-side work. But the earlier controller-side prep in the builtin copy action is still there. ([GitHub][8])

So the implementer’s truth is:

* **template** can be deeply accelerated with no task rewrite;
* **explicit builtin copy** can be broadly accelerated by B and partially by module shims;
* a full semantic replacement for `ansible.builtin.copy` with zero task changes needs either unqualified/legacy task names or a patched `ansible-core`.

### 4) `package`: let core pick the manager, then shadow the manager

The builtin `package` module is already a controller-side proxy. The docs say it may run `setup`, and the current action source shows it reads `ansible_package_use` or facts, may call `ansible.legacy.setup`, then prefixes builtin package-manager modules with `ansible.legacy.` before executing them. ([Ansible Documentation][9])

That gives you two implementable choices:

* **Transparent path for generic `package` tasks:** shadow `apt`, `dnf`, `yum`, `zypper`, and so on in `library/`. Each shim translates the module args into a `PackageRequest{manager, names, state}` RPC to the agent.
* **One-module path with minimal config change:** set `ansible_package_use=fastpkg` in inventory so the builtin `package` action routes all generic `package` tasks to one custom module. That changes inventory/config, not the task text. ([Ansible Documentation][9])

This is a very good target for Go binary modules plus a resident agent.

### 5) `service`: same pattern as `package`

The builtin `service` module is also a controller-side proxy. The docs say it may use `setup`; current source shows it resolves `ansible_service_mgr`, may call `ansible.legacy.setup`, then dispatches to `ansible.legacy.systemd`, `ansible.legacy.sysvinit`, or `ansible.legacy.service`. ([Ansible Documentation][10])

So the same implementation pattern works:

* shadow `systemd`, `sysvinit`, and maybe `service` in `library/`, or
* use a custom manager name through the existing `use` mechanism if you are okay with an inventory/task-level knob. ([Ansible Documentation][10])

### 6) `file` and `stat`: usually B first, shim later

`file` does not advertise a corresponding action plugin the way `copy`, `template`, `package`, and `service` do, so it is not the first place I would spend controller-side complexity. B already accelerates it, and a local `file` module shim is straightforward later. Also, overriding `file` helps the builtin copy path, because the current copy action calls `ansible.legacy.file` internally. ([Ansible Documentation][11])

## Why this split is implementable

This split keeps you on the supported hooks and also minimizes the amount of Ansible behavior you reimplement.

* **B** gives you a supported, host-wide execution substrate. ([Ansible Documentation][4])
* **Action overrides** are reserved for controller-side semantics like templating. Ansible explicitly documents that action plugins do controller-side preprocessing, and `template` is the canonical example. ([Ansible Documentation][12])
* **Module shims** are used where builtin actions already dispatch to `ansible.legacy.<module>`, and binary modules are officially supported. ([Ansible Documentation][1])

That last point is especially important because action plugins that bypass `_execute_module()` have to take care with result handling. The module architecture docs note that normal module execution marks returned strings unsafe and handles internal arguments such as `_ansible_no_log`, `_ansible_debug`, `_ansible_diff`, `_ansible_tmpdir`, and `_ansible_remote_tmp`. If you stay on the module path where possible, Ansible keeps doing that work for you. ([Ansible Documentation][1])

## The RPC surface I would standardize

Use one agent API and let the shim/action layer map into it:

```text
Hello {version, capabilities}
Exec {argv?, cmd_string?, use_shell, cwd, env, stdin, timeout, creates, removes}
Stat {path, follow, checksum}
WriteFileIfChanged {dest, bytes|blob_id, owner, group, mode, secontext, backup, unsafe_writes, validate}
ReadFile {path}
Package {manager, names, state}
Service {manager, name, state, enabled}
File {path, state, owner, group, mode, recurse, follow, mtime, atime}
```

That is an implementation recommendation, not something Ansible dictates.

## The shortest path to a working prototype

1. **Build B only** and make `raw`, ordinary module execution, and file transfer work through `fastagent`.
2. Add a **`command` module shim**; that buys you both `command` and `shell` transparently. ([GitHub][6])
3. Add a **`copy` action override**; that buys you `template` fast paths immediately because builtin template already delegates to `ansible.legacy.copy`. ([GitHub][13])
4. Add **package-manager shims** and **service-manager shims**.
5. Decide whether explicit `ansible.builtin.copy` is common enough to justify a small `ansible-core` fork.

The one big caveat is still task authoring style: codebases that already lean on generic `package` / `service`, `template`, and unqualified module names are much easier to accelerate transparently than codebases that hard-pin every task to `ansible.builtin.copy`, `ansible.builtin.apt`, and similar builtin FQCNs. That follows directly from Ansible’s `ansible.legacy` vs `ansible.builtin` resolution rules. ([Ansible Documentation][2])

If I were handing this to an engineer, I’d summarize it in one sentence:

**Implement `fastagent` as a connection plugin first, then add repo-local `ansible.legacy` shims where core already dispatches there (`command`, package managers, service managers), and use one `copy` action override specifically to accelerate the builtin `template` path.**

[1]: https://docs.ansible.com/projects/ansible/latest/dev_guide/developing_program_flow_modules.html "https://docs.ansible.com/projects/ansible/latest/dev_guide/developing_program_flow_modules.html"
[2]: https://docs.ansible.com/projects/ansible/latest/reference_appendices/faq.html "https://docs.ansible.com/projects/ansible/latest/reference_appendices/faq.html"
[3]: https://docs.ansible.com/projects/ansible/latest/plugins/action.html "https://docs.ansible.com/projects/ansible/latest/plugins/action.html"
[4]: https://docs.ansible.com/projects/ansible/latest/plugins/connection.html "https://docs.ansible.com/projects/ansible/latest/plugins/connection.html"
[5]: https://docs.ansible.com/projects/ansible/latest/collections/ansible/builtin/raw_module.html "https://docs.ansible.com/projects/ansible/latest/collections/ansible/builtin/raw_module.html"
[6]: https://github.com/ansible/ansible/blob/devel/lib/ansible/plugins/action/command.py "https://github.com/ansible/ansible/blob/devel/lib/ansible/plugins/action/command.py"
[7]: https://docs.ansible.com/projects/ansible/latest/collections/ansible/builtin/command_module.html "https://docs.ansible.com/projects/ansible/latest/collections/ansible/builtin/command_module.html"
[8]: https://github.com/ansible/ansible/blob/devel/lib/ansible/plugins/action/copy.py "https://github.com/ansible/ansible/blob/devel/lib/ansible/plugins/action/copy.py"
[9]: https://docs.ansible.com/projects/ansible/latest/collections/ansible/builtin/package_module.html "https://docs.ansible.com/projects/ansible/latest/collections/ansible/builtin/package_module.html"
[10]: https://docs.ansible.com/projects/ansible/latest/collections/ansible/builtin/service_module.html "https://docs.ansible.com/projects/ansible/latest/collections/ansible/builtin/service_module.html"
[11]: https://docs.ansible.com/projects/ansible/latest/collections/ansible/builtin/file_module.html "https://docs.ansible.com/projects/ansible/latest/collections/ansible/builtin/file_module.html"
[12]: https://docs.ansible.com/projects/ansible/latest/dev_guide/developing_plugins.html "https://docs.ansible.com/projects/ansible/latest/dev_guide/developing_plugins.html"
[13]: https://github.com/ansible/ansible/blob/devel/lib/ansible/plugins/action/template.py "https://github.com/ansible/ansible/blob/devel/lib/ansible/plugins/action/template.py"

