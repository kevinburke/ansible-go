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
                default="present",
                choices=["present", "absent", "latest", "installed", "removed"],
            ),
            # Accept common dnf parameters for compatibility.
            enablerepo=dict(type="list", elements="str", default=[]),
            disablerepo=dict(type="list", elements="str", default=[]),
            conf_file=dict(type="str"),
            disable_gpg_check=dict(type="bool", default=False),
            installroot=dict(type="str", default="/"),
            releasever=dict(type="str"),
            autoremove=dict(type="bool", default=False),
            exclude=dict(type="list", elements="str", default=[]),
            skip_broken=dict(type="bool", default=False),
            update_cache=dict(type="bool", default=False),
            security=dict(type="bool", default=False),
            bugfix=dict(type="bool", default=False),
            nobest=dict(type="bool", default=False),
            cacheonly=dict(type="bool", default=False),
            lock_timeout=dict(type="int", default=30),
            install_weak_deps=dict(type="bool", default=True),
            download_only=dict(type="bool", default=False),
            allowerasing=dict(type="bool", default=False),
            download_dir=dict(type="str"),
            sslverify=dict(type="bool", default=True),
        ),
        supports_check_mode=True,
    )

    names = module.params.get("name") or []
    state = module.params["state"]

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
