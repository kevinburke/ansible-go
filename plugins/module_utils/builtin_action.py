"""Load ansible-core's own action plugins, whatever shadows them."""

from __future__ import annotations

import importlib.util
import os

import ansible.plugins.action as _ansible_action_pkg
from ansible.errors import AnsibleActionFail


def load_builtin_action_class(name):
    """Return ansible-core's builtin `name` ActionModule class.

    We can't use `from ansible.plugins.action.copy import ActionModule`:
    callers that put fastagent's action plugins on the legacy
    `action_plugins` search path (e.g. caracal-server's `ansible.cfg`, to
    shadow unqualified `copy:`) cause ansible's PluginLoader to register
    *our* file under `sys.modules["ansible.plugins.action.copy"]`, aliasing
    over the real builtin. The import then resolves back into our
    partially-loaded module and fails with `ImportError: cannot import name
    'ActionModule' from 'ansible.plugins.action.copy' (…/kevinburke/…/copy.py)`.

    We can't use `action_loader.get("ansible.legacy.copy")` either: under
    the same legacy-path shadowing, `ansible.legacy.copy` resolves to our
    class, so a fallback recurses until CPython raises
    `RecursionError: maximum recursion depth exceeded`.

    Instead, find the real file on disk via the parent package's
    `__path__` (which is not mutated by legacy-plugin registration) and
    load it with `importlib.util.spec_from_file_location` under a name
    that can't clash with anything in `sys.modules`.
    """
    for base in _ansible_action_pkg.__path__:
        candidate = os.path.join(base, f"{name}.py")
        if os.path.isfile(candidate):
            spec = importlib.util.spec_from_file_location(
                f"kevinburke.fastagent._builtin_{name}_action", candidate
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod.ActionModule
    raise AnsibleActionFail(
        f"fastagent: could not locate ansible-core's builtin {name} "
        f"action plugin under {list(_ansible_action_pkg.__path__)}"
    )
