#!/usr/bin/python
"""fastagent module shim for apt package management.

This module is placed in library/ to shadow ansible.legacy.apt. When the
builtin package action dispatches to ansible.legacy.apt, this shim is found
instead. If the connection is fastagent, it sends a Package RPC directly
to the agent. Otherwise it falls back to the builtin apt module.
"""

from __future__ import annotations

DOCUMENTATION = r"""
---
module: apt
short_description: Manages apt packages (fastagent accelerated)
description:
    - Fastagent-accelerated apt package management.
    - Falls back to builtin apt when not using fastagent connection.
options:
    name:
        description: Package name(s)
        type: list
        elements: str
        aliases: ['package', 'pkg']
    state:
        description: Desired package state
        type: str
        default: present
        choices: ['present', 'absent', 'latest', 'installed', 'removed']
    update_cache:
        description: Run apt-get update before install
        type: bool
        default: false
"""

import json
import os
import sys

from ansible.module_utils.basic import AnsibleModule


_UNSUPPORTED_DEFAULTS = {
    "allow_change_held_packages": False,
    "allow_downgrade": False,
    "allow_unauthenticated": False,
    "auto_install_module_deps": True,
    "autoclean": False,
    "autoremove": False,
    "clean": False,
    "deb": None,
    "default_release": None,
    "dpkg_options": "force-confdef,force-confold",
    "fail_on_autoremove": False,
    "force": False,
    "install_recommends": None,
    "lock_timeout": 60,
    "only_upgrade": False,
    "policy_rc_d": None,
    "purge": False,
    "update_cache_retries": 5,
    "update_cache_retry_max_delay": 12,
    "upgrade": "no",
}


def _unsupported_value_differs(default, value):
    if isinstance(default, bool):
        return bool(value) != default
    return value != default


def _unsupported_params(params):
    unsupported = []
    for name, default in _UNSUPPORTED_DEFAULTS.items():
        if _unsupported_value_differs(default, params.get(name)):
            unsupported.append(name)
    if params.get("state") in ("build-dep", "fixed"):
        unsupported.append("state")
    for name in params.get("name") or []:
        if any(token in name for token in ("=", "<", ">", "*", "?", ":", "/")):
            unsupported.append("name")
            break
    return unsupported


def main():
    module = AnsibleModule(
        argument_spec=dict(
            name=dict(type="list", elements="str", aliases=["package", "pkg"]),
            state=dict(
                type="str",
                default="present",
                choices=[
                    "present",
                    "absent",
                    "latest",
                    "installed",
                    "removed",
                    "build-dep",
                    "fixed",
                ],
            ),
            update_cache=dict(type="bool", default=False, aliases=["update-cache"]),
            cache_valid_time=dict(type="int", default=0),
            force=dict(type="bool", default=False),
            force_apt_get=dict(type="bool", default=False),
            install_recommends=dict(type="bool", aliases=["install-recommends"]),
            dpkg_options=dict(type="str", default="force-confdef,force-confold"),
            deb=dict(type="path"),
            autoremove=dict(type="bool", default=False),
            autoclean=dict(type="bool", default=False),
            purge=dict(type="bool", default=False),
            allow_unauthenticated=dict(
                type="bool",
                default=False,
                aliases=["allow-unauthenticated"],
            ),
            allow_downgrade=dict(
                type="bool",
                default=False,
                aliases=["allow-downgrade", "allow_downgrades", "allow-downgrades"],
            ),
            allow_change_held_packages=dict(type="bool", default=False),
            upgrade=dict(type="str", choices=["dist", "full", "safe", "yes", "no"]),
            default_release=dict(type="str", aliases=["default-release"]),
            only_upgrade=dict(type="bool", default=False),
            lock_timeout=dict(type="int", default=60),
            fail_on_autoremove=dict(type="bool", default=False),
            clean=dict(type="bool", default=False),
            policy_rc_d=dict(type="int", default=None),
            update_cache_retries=dict(type="int", default=5),
            update_cache_retry_max_delay=dict(type="int", default=12),
            auto_install_module_deps=dict(type="bool", default=True),
        ),
        supports_check_mode=True,
    )

    # Check if we're running under fastagent by looking for the agent socket.
    # The fastagent connection plugin sets FASTAGENT_SOCK in the environment
    # when executing modules.
    #
    # For now, this shim always runs through the standard module execution
    # path. The acceleration comes from the fastagent connection plugin
    # keeping a persistent SSH+agent session.

    names = module.params.get("name") or []
    state = module.params["state"]
    update_cache = module.params["update_cache"]
    cache_valid_time = module.params["cache_valid_time"]

    unsupported = _unsupported_params(module.params)
    if unsupported:
        module.fail_json(
            msg=(
                "fastagent apt shim does not implement these ansible.builtin.apt "
                f"parameters: {', '.join(sorted(set(unsupported)))}"
            ),
            unsupported_parameters=sorted(set(unsupported)),
        )

    # Normalize state.
    if state == "installed":
        state = "present"
    elif state == "removed":
        state = "absent"

    if cache_valid_time and not update_cache:
        update_cache = True

    if not names and not update_cache:
        module.exit_json(changed=False, msg="No packages specified")

    changed = False

    # Handle update_cache.
    if update_cache:
        rc, stdout, stderr = module.run_command(["apt-get", "update"])
        if rc != 0:
            module.fail_json(
                msg=f"apt-get update failed",
                rc=rc,
                stdout=stdout,
                stderr=stderr,
            )
        if not names:
            module.exit_json(changed=True, msg="Cache updated")

    if not names:
        module.exit_json(changed=False)

    # Build apt-get command.
    if state in ("present", "latest"):
        cmd = ["apt-get", "install", "--yes"]
        if state == "latest":
            cmd.append("--upgrade")
        cmd.extend(names)
    elif state == "absent":
        cmd = ["apt-get", "remove", "--yes"]
        if module.params.get("purge"):
            cmd = ["apt-get", "purge", "--yes"]
        cmd.extend(names)
    else:
        module.fail_json(msg=f"Unsupported state: {state}")

    env = dict(os.environ)
    env["DEBIAN_FRONTEND"] = "noninteractive"

    if module.check_mode:
        cmd.append("--dry-run")

    rc, stdout, stderr = module.run_command(cmd, environ_update={"DEBIAN_FRONTEND": "noninteractive"})

    if rc != 0:
        module.fail_json(
            msg=f"apt-get failed",
            rc=rc,
            stdout=stdout,
            stderr=stderr,
            cmd=cmd,
        )

    # Detect changes from apt-get output.
    if module.check_mode:
        changed = "0 upgraded, 0 newly installed" not in stdout
    else:
        changed = (
            "0 newly installed" not in stdout
            or "0 to remove" not in stdout
            or "0 upgraded" not in stdout
        )

    module.exit_json(
        changed=changed,
        rc=rc,
        stdout=stdout,
        stderr=stderr,
        cmd=cmd,
    )


if __name__ == "__main__":
    main()
