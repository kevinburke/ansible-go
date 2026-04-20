#!/usr/bin/python
"""Routing-only shim for the file module.

See plugins/modules/copy.py for the rationale. The action plugin at
plugins/action/file.py performs all execution; this stub exists solely so
the `collections:` play keyword resolves unqualified `file:` tasks to our
collection and selects the action plugin override.
"""

from __future__ import annotations

DOCUMENTATION = r"""
---
module: file
short_description: Routing shim for kevinburke.fastagent file override
description:
    - Stub module that exists so the C(collections:) play keyword routes
      unqualified C(file:) tasks to the fastagent action plugin override.
options: {}
"""

import json
import sys


def main() -> None:
    print(json.dumps({
        "failed": True,
        "msg": (
            "kevinburke.fastagent.file shim was invoked directly. The action "
            "plugin override should have handled this task. Fallbacks must "
            "pass module_name='ansible.builtin.file' to _execute_module()."
        ),
    }))
    sys.exit(1)


if __name__ == "__main__":
    main()
