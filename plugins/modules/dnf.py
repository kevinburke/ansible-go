#!/usr/bin/python
"""fastagent module shim for dnf/yum package management.

This module is placed in library/ to shadow ansible.legacy.dnf. When the
builtin package action dispatches to ansible.legacy.dnf, this shim is found
instead.
"""

from __future__ import annotations

DOCUMENTATION = r"""
---
module: dnf
short_description: Manages dnf/yum packages (fastagent accelerated)
description:
    - Fastagent-accelerated dnf package management.
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
"""

import os

from ansible.module_utils.basic import AnsibleModule


_UNSUPPORTED_DEFAULTS = {
    "allow_downgrade": False,
    "allowerasing": False,
    "autoremove": False,
    "best": None,
    "bugfix": False,
    "cacheonly": False,
    "conf_file": None,
    "disable_excludes": None,
    "disable_gpg_check": False,
    "disable_plugin": [],
    "disablerepo": [],
    "download_dir": None,
    "download_only": False,
    "enable_plugin": [],
    "enablerepo": [],
    "exclude": [],
    "installroot": "/",
    "install_weak_deps": True,
    "list": None,
    "lock_timeout": 30,
    "nobest": None,
    "releasever": None,
    "security": False,
    "skip_broken": False,
    "sslverify": True,
    "update_cache": False,
    "update_only": False,
    "use_backend": "auto",
    "validate_certs": True,
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
    for name in params.get("name") or []:
        if any(
            token in name
            for token in (" ", ">", "<", "=", "*", "?", ":", "/", "@")
        ):
            unsupported.append("name")
            break
    return unsupported


def _find_manager():
    """Find the available package manager (dnf or yum)."""
    for mgr in ("dnf", "yum"):
        for path in ("/usr/bin", "/bin", "/usr/sbin"):
            if os.path.isfile(os.path.join(path, mgr)):
                return mgr
    return "dnf"


def main():
    module = AnsibleModule(
        argument_spec=dict(
            name=dict(type="list", elements="str", aliases=["package", "pkg"]),
            state=dict(
                type="str",
                default=None,
                choices=["present", "absent", "latest", "installed", "removed"],
            ),
            # Accept common dnf parameters so unsupported values fail before
            # package operations instead of being silently ignored.
            allow_downgrade=dict(type="bool", default=False),
            allowerasing=dict(type="bool", default=False),
            best=dict(type="bool"),
            list=dict(type="str"),
            use_backend=dict(
                type="str",
                default="auto",
                choices=["auto", "dnf", "yum", "yum4", "dnf4", "dnf5"],
            ),
            enablerepo=dict(type="list", elements="str", default=[]),
            disablerepo=dict(type="list", elements="str", default=[]),
            conf_file=dict(type="str"),
            disable_gpg_check=dict(type="bool", default=False),
            disable_excludes=dict(type="str", default=None),
            disable_plugin=dict(type="list", elements="str", default=[]),
            enable_plugin=dict(type="list", elements="str", default=[]),
            installroot=dict(type="str", default="/"),
            releasever=dict(type="str"),
            autoremove=dict(type="bool", default=False),
            exclude=dict(type="list", elements="str", default=[]),
            skip_broken=dict(type="bool", default=False),
            update_cache=dict(type="bool", default=False, aliases=["expire-cache"]),
            security=dict(type="bool", default=False),
            bugfix=dict(type="bool", default=False),
            nobest=dict(type="bool"),
            cacheonly=dict(type="bool", default=False),
            lock_timeout=dict(type="int", default=30),
            install_weak_deps=dict(type="bool", default=True),
            download_only=dict(type="bool", default=False),
            download_dir=dict(type="str"),
            update_only=dict(type="bool", default=False),
            validate_certs=dict(type="bool", default=True),
            sslverify=dict(type="bool", default=True),
        ),
        supports_check_mode=True,
    )

    names = module.params.get("name") or []
    state = module.params["state"]
    if state is None:
        state = "absent" if module.params.get("autoremove") else "present"

    unsupported = _unsupported_params(module.params)
    if unsupported:
        module.fail_json(
            msg=(
                "fastagent dnf shim does not implement these ansible.builtin.dnf "
                f"parameters: {', '.join(sorted(set(unsupported)))}"
            ),
            unsupported_parameters=sorted(set(unsupported)),
        )

    # Normalize state.
    if state == "installed":
        state = "present"
    elif state == "removed":
        state = "absent"

    if not names:
        module.exit_json(changed=False, msg="No packages specified")

    manager = _find_manager()

    # Build command.
    if state in ("present", "latest"):
        cmd = [manager, "install", "-y"]
        if state == "latest":
            cmd.append("--best")
        cmd.extend(names)
    elif state == "absent":
        cmd = [manager, "remove", "-y"]
        cmd.extend(names)
    else:
        module.fail_json(msg=f"Unsupported state: {state}")

    if module.check_mode:
        cmd.append("--assumeno")

    rc, stdout, stderr = module.run_command(cmd)

    if rc != 0:
        module.fail_json(
            msg=f"{manager} failed",
            rc=rc,
            stdout=stdout,
            stderr=stderr,
            cmd=cmd,
        )

    changed = "Nothing to do" not in stdout and "Complete!" in stdout

    module.exit_json(
        changed=changed,
        rc=rc,
        stdout=stdout,
        stderr=stderr,
        cmd=cmd,
    )


if __name__ == "__main__":
    main()
