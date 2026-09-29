"""fastagent action plugin override for template module.

Stock template renders on the controller and hands the result to the copy
action (`ansible.legacy.copy`, which fastagent overrides). Because it sets
TRANSFERS_FILES, ActionBase.run first makes a remote tmp dir, which costs
an Exec to expand `~` and an Exec to mkdir, and its `finally` removes the
dir with a third. The fastagent copy fast path never uses that dir, since
WriteFile carries the content, so on a fastagent connection this skips it.
With copy's single WriteFile, an unchanged template is one RPC instead of
five.

Everything else, including rendering, is ansible-core's template action.
"""

from __future__ import annotations

try:
    from ansible_collections.kevinburke.fastagent.plugins.module_utils.builtin_action import (
        load_builtin_action_class,
    )
except ImportError:
    from plugins.module_utils.builtin_action import load_builtin_action_class


class ActionModule(load_builtin_action_class("template")):

    def _early_needs_tmp_path(self):
        if self._connection.transport == "fastagent":
            return False
        return super()._early_needs_tmp_path()

    def run(self, tmp=None, task_vars=None):
        if self._connection.transport != "fastagent":
            return super().run(tmp, task_vars)

        shell = self._connection._shell
        tmpdir_before = shell.tmpdir
        try:
            return super().run(tmp, task_vars)
        finally:
            # When copy falls back to ansible-core's copy action, that
            # action makes its own tmp dir. It removes the dir when it
            # succeeds but not when it fails, relying, as stock does, on
            # template's `finally` to remove the dir template made. Stock
            # template's `finally` removes only a dir this instance made,
            # so remove a dir the fallback left here.
            if tmpdir_before is None and shell.tmpdir is not None:
                self._cleanup_remote_tmp = True
                self._remove_tmp_path(shell.tmpdir)
