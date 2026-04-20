#!/usr/bin/python
"""Routing-only shim for the command module. See copy.py for the rationale."""

from __future__ import annotations

DOCUMENTATION = r"""
---
module: command
short_description: Routing shim for kevinburke.fastagent command override
description:
    - Stub module that exists so the C(collections:) play keyword routes
      unqualified C(command:) tasks to the fastagent action plugin override.
options: {}
"""

import json
import sys


def main() -> None:
    print(json.dumps({
        "failed": True,
        "msg": (
            "kevinburke.fastagent.command shim was invoked directly. The action "
            "plugin override should have handled this task. Fallbacks must "
            "pass module_name='ansible.legacy.command' (or ansible.builtin) "
            "to _execute_module()."
        ),
    }))
    sys.exit(1)


if __name__ == "__main__":
    main()
