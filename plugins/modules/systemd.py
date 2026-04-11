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
        required_one_of=[["name", "daemon_reload"]],
    )

    name = module.params["name"]
    state = module.params["state"]
    enabled = module.params["enabled"]
    daemon_reload = module.params["daemon_reload"]
    masked = module.params["masked"]
    scope = module.params["scope"]
    no_block = module.params["no_block"]

    changed = False
    result = {"name": name, "changed": False}

    systemctl = "systemctl"
    if scope == "user":
        systemctl_cmd = [systemctl, "--user"]
    elif scope == "global":
        systemctl_cmd = [systemctl, "--global"]
    else:
        systemctl_cmd = [systemctl]

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
        changed = True

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
        action = None
        needs_action = False

        if state == "started" and current_active != "active":
            action = "start"
            needs_action = True
        elif state == "stopped" and current_active == "active":
            action = "stop"
            needs_action = True
        elif state == "restarted":
            action = "restart"
            needs_action = True
        elif state == "reloaded":
            action = "reload"
            needs_action = True

        if needs_action and not module.check_mode:
            cmd = systemctl_cmd + [action, name]
            if no_block:
                cmd.append("--no-block")
            rc, stdout, stderr = module.run_command(cmd)
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
