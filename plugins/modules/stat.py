#!/usr/bin/python
"""Routing-only shim for the stat module. See copy.py for the rationale."""

from __future__ import annotations

DOCUMENTATION = r"""
---
module: stat
short_description: Routing shim for kevinburke.fastagent stat override
description:
    - Stub module that exists so the C(collections:) play keyword routes
      unqualified C(stat:) tasks to the fastagent action plugin override.
options: {}
"""

import json
import sys


def main() -> None:
    print(json.dumps({
        "failed": True,
        "msg": (
            "kevinburke.fastagent.stat shim was invoked directly. The action "
            "plugin override should have handled this task. Fallbacks must "
            "pass module_name='ansible.builtin.stat' to _execute_module()."
        ),
    }))
    sys.exit(1)


if __name__ == "__main__":
    main()
