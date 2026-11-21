#!/usr/bin/python
"""Routing-only shim for the slurp module. See copy.py for the rationale."""

from __future__ import annotations

DOCUMENTATION = r"""
---
module: slurp
short_description: Routing shim for kevinburke.fastagent slurp override
description:
    - Stub module that exists so the C(collections:) play keyword routes
      unqualified C(slurp:) tasks to the fastagent action plugin override.
options: {}
"""

import json
import sys


def main() -> None:
    print(json.dumps({
        "failed": True,
        "msg": (
            "kevinburke.fastagent.slurp shim was invoked directly. The action "
            "plugin override should have handled this task. Fallbacks must "
            "pass module_name='ansible.builtin.slurp' to _execute_module()."
        ),
    }))
    sys.exit(1)


if __name__ == "__main__":
    main()
