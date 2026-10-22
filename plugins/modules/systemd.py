#!/usr/bin/python
"""fastagent module shim for systemd service management.

This module is placed in library/ to shadow ansible.legacy.systemd. When the
builtin service action dispatches to ansible.legacy.systemd, this shim is found
instead.
"""

from __future__ import annotations

DOCUMENTATION = r"""
---
module: systemd
short_description: Manages systemd services (fastagent accelerated)
description:
    - Fastagent-accelerated systemd service management.
options:
    name:
        description: Service name
        type: str
        aliases: ['service', 'unit']
    state:
        description: Desired service state
        type: str
        choices: ['started', 'stopped', 'restarted', 'reloaded']
    enabled:
        description: Whether the service should be enabled
        type: bool
    daemon_reload:
        description: Run systemctl daemon-reload
        type: bool
        default: false
    scope:
        description: Systemd scope
        type: str
        choices: ['system', 'user', 'global']
        default: system
    masked:
        description: Whether the service should be masked
        type: bool
    no_block:
        description: Do not wait for operation to complete
        type: bool
        default: false
"""

from ansible.module_utils.basic import AnsibleModule

def state_action(state, active):
    """Return the systemctl verb that brings a unit whose `systemctl
    is-active` state is active to the requested state, or None.

    Mirrors upstream Ansible's systemd_service module: a unit is running when
    it is active or activating (a crash-looping unit under Restart= is
    activating between attempts); stopped also stops a deactivating unit;
    restarted and reloaded start a unit that is not running. Keep in sync
    with systemdStateAction in service.go; both are checked against
    testdata/systemd_state_actions.json.
    """
    running = active in ("active", "activating")
    if state == "started":
        return None if running else "start"
    if state == "stopped":
        return "stop" if running or active == "deactivating" else None
    if state in ("restarted", "reloaded"):
        return state[: -len("ed")] if running else "start"
    raise ValueError(f"unsupported service state: {state!r}")


def main():
    module = AnsibleModule(
        argument_spec=dict(
            name=dict(type="str", aliases=["service", "unit"]),
            state=dict(
                type="str",
                choices=["started", "stopped", "restarted", "reloaded"],
            ),
            enabled=dict(type="bool"),
            daemon_reload=dict(type="bool", default=False),
            daemon_reexec=dict(type="bool", default=False),
            scope=dict(type="str", choices=["system", "user", "global"], default="system"),
            masked=dict(type="bool"),
            no_block=dict(type="bool", default=False),
            force=dict(type="bool", default=False),
        ),
        supports_check_mode=True,
        required_one_of=[["state", "enabled", "masked", "daemon_reload", "daemon_reexec"]],
        required_by=dict(
            state=("name",),
            enabled=("name",),
            masked=("name",),
        ),
    )

    name = module.params["name"]
    state = module.params["state"]
    enabled = module.params["enabled"]
    daemon_reload = module.params["daemon_reload"]
    daemon_reexec = module.params["daemon_reexec"]
    masked = module.params["masked"]
    scope = module.params["scope"]
    no_block = module.params["no_block"]
    force = module.params["force"]

    changed = False
    result = {"name": name, "changed": False}

    systemctl = "systemctl"
    if scope == "user":
        systemctl_cmd = [systemctl, "--user"]
    elif scope == "global":
        systemctl_cmd = [systemctl, "--global"]
    else:
        systemctl_cmd = [systemctl]
    if no_block:
        systemctl_cmd.append("--no-block")
    if force:
        systemctl_cmd.append("--force")

    # daemon-reload
    if daemon_reload:
        if not module.check_mode:
            rc, stdout, stderr = module.run_command(systemctl_cmd + ["daemon-reload"])
            if rc != 0:
                module.fail_json(
                    msg="daemon-reload failed",
                    rc=rc,
                    stdout=stdout,
                    stderr=stderr,
                )

    # daemon-reexec
    if daemon_reexec:
        if not module.check_mode:
            rc, stdout, stderr = module.run_command(systemctl_cmd + ["daemon-reexec"])
            if rc != 0:
                module.fail_json(
                    msg="daemon-reexec failed",
                    rc=rc,
                    stdout=stdout,
                    stderr=stderr,
                )

    if not name:
        result["changed"] = changed
        module.exit_json(**result)

    # Get current state.
    rc, active_out, _ = module.run_command(systemctl_cmd + ["is-active", name])
    current_active = active_out.strip()

    rc, enabled_out, _ = module.run_command(systemctl_cmd + ["is-enabled", name])
    current_enabled = enabled_out.strip() == "enabled"

    result["status"] = {"ActiveState": current_active, "UnitFileState": enabled_out.strip()}

    # Handle masked.
    if masked is not None:
        rc, mask_out, _ = module.run_command(systemctl_cmd + ["is-enabled", name])
        is_masked = mask_out.strip() == "masked"
        if masked and not is_masked:
            if not module.check_mode:
                rc, stdout, stderr = module.run_command(systemctl_cmd + ["mask", name])
                if rc != 0:
                    module.fail_json(msg="mask failed", rc=rc, stdout=stdout, stderr=stderr)
            changed = True
        elif not masked and is_masked:
            if not module.check_mode:
                rc, stdout, stderr = module.run_command(systemctl_cmd + ["unmask", name])
                if rc != 0:
                    module.fail_json(msg="unmask failed", rc=rc, stdout=stdout, stderr=stderr)
            changed = True

    # Handle state.
    if state is not None:
        action = state_action(state, current_active)
        needs_action = action is not None

        # Like upstream, report the change in check mode without running it.
        if needs_action:
            if not module.check_mode:
                rc, stdout, stderr = module.run_command(systemctl_cmd + [action, name])
                if rc != 0:
                    module.fail_json(
                        msg=f"Unable to {action} service {name}",
                        rc=rc,
                        stdout=stdout,
                        stderr=stderr,
                    )
            changed = True

    # Handle enabled.
    if enabled is not None and enabled != current_enabled:
        action = "enable" if enabled else "disable"
        if not module.check_mode:
            rc, stdout, stderr = module.run_command(systemctl_cmd + [action, name])
            if rc != 0:
                module.fail_json(
                    msg=f"Unable to {action} service {name}",
                    rc=rc,
                    stdout=stdout,
                    stderr=stderr,
                )
        changed = True

    result["changed"] = changed
    if state:
        result["state"] = state
    if enabled is not None:
        result["enabled"] = enabled

    module.exit_json(**result)


if __name__ == "__main__":
    main()
