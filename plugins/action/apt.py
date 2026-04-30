"""fastagent action plugin override for apt module.

When using the fastagent connection, this sends a Package RPC directly to the
agent instead of transferring and executing the Python module.
For non-fastagent connections, falls back to normal module execution.
"""

from __future__ import annotations

from ansible.plugins.action import ActionBase
from ansible.module_utils.parsing.convert_bool import boolean
from ansible.utils.vars import merge_hash


_APT_SUPPORTED_ARGS = {
    "name",
    "package",
    "pkg",
    "state",
    "update_cache",
    "update-cache",
    "cache_valid_time",
}

_APT_UNSUPPORTED_ARGS = {
    "allow_change_held_packages",
    "allow_downgrade",
    "allow-downgrade",
    "allow_downgrades",
    "allow-downgrades",
    "allow_unauthenticated",
    "allow-unauthenticated",
    "auto_install_module_deps",
    "autoclean",
    "autoremove",
    "clean",
    "deb",
    "default_release",
    "default-release",
    "dpkg_options",
    "fail_on_autoremove",
    "force",
    "force_apt_get",
    "install_recommends",
    "install-recommends",
    "lock_timeout",
    "only_upgrade",
    "policy_rc_d",
    "purge",
    "update_cache_retries",
    "update_cache_retry_max_delay",
    "upgrade",
}

_APT_UNSUPPORTED_DEFAULTS = {
    "allow_change_held_packages": False,
    "allow_downgrade": False,
    "allow-downgrade": False,
    "allow_downgrades": False,
    "allow-downgrades": False,
    "allow_unauthenticated": False,
    "allow-unauthenticated": False,
    "auto_install_module_deps": True,
    "autoclean": False,
    "autoremove": False,
    "clean": False,
    "deb": None,
    "default_release": None,
    "default-release": None,
    "dpkg_options": "force-confdef,force-confold",
    "fail_on_autoremove": False,
    "force": False,
    "force_apt_get": False,
    "install_recommends": None,
    "install-recommends": None,
    "lock_timeout": 60,
    "only_upgrade": False,
    "policy_rc_d": None,
    "purge": False,
    "update_cache_retries": 5,
    "update_cache_retry_max_delay": 12,
    "upgrade": "no",
}


def _fallback_to_builtin(action, result, task_vars):
    return merge_hash(
        result,
        action._execute_module(
            module_name="ansible.builtin.apt", task_vars=task_vars
        ),
    )


def _truthy_arg(value):
    if isinstance(value, bool):
        return value
    return boolean(value, strict=False)


def _arg_differs_from_default(name, value):
    default = _APT_UNSUPPORTED_DEFAULTS[name]
    if isinstance(default, bool):
        return _truthy_arg(value) != default
    return value != default


def _should_fallback(args):
    for name, value in args.items():
        if name in _APT_SUPPORTED_ARGS:
            continue
        if name in _APT_UNSUPPORTED_ARGS:
            if _arg_differs_from_default(name, value):
                return True
            continue
        return True

    state = args.get("state", "present")
    if state in ("build-dep", "fixed"):
        return True

    names = args.get("name") or args.get("package") or args.get("pkg") or []
    if isinstance(names, str):
        names = [names]
    for name in names:
        if any(token in name for token in ("=", "<", ">", "*", "?", ":", "/")):
            return True

    return False


class ActionModule(ActionBase):

    def run(self, tmp=None, task_vars=None):
        if task_vars is None:
            task_vars = dict()

        result = super().run(tmp, task_vars)
        del tmp

        if self._connection.transport != "fastagent":
            return _fallback_to_builtin(self, result, task_vars)

        self._connection._connect()

        args = self._task.args
        if _should_fallback(args):
            return _fallback_to_builtin(self, result, task_vars)

        names = args.get("name") or args.get("package") or args.get("pkg") or []
        if isinstance(names, str):
            names = [names]
        state = args.get("state", "present")
        update_cache = boolean(
            args.get("update_cache", args.get("update-cache", False)),
            strict=False,
        )
        cache_valid_time = args.get("cache_valid_time", 0)
        if (
            cache_valid_time
            and "update_cache" not in args
            and "update-cache" not in args
        ):
            update_cache = True

        # Normalize state.
        if state == "installed":
            state = "present"
        elif state == "removed":
            state = "absent"

        client = self._connection._agent_client
        check_mode = self._play_context.check_mode

        if check_mode:
            return _fallback_to_builtin(self, result, task_vars)

        # Send everything to the Package RPC — it handles update_cache
        # deduplication internally (skips if cache was updated recently).
        try:
            pkg_result = client.call("Package", {
                "manager": "apt",
                "names": names,
                "state": state,
                "update_cache": update_cache,
                "cache_valid_time": cache_valid_time,
            })
            result["changed"] = pkg_result.get("changed", False)
            result["cache_updated"] = pkg_result.get("cache_updated", False)
            result["msg"] = pkg_result.get("msg", "")
        except Exception as e:
            result["failed"] = True
            result["msg"] = f"fastagent apt failed: {e}"

        return result
