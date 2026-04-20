#!/usr/bin/python
"""Routing-only shim for the copy module.

This file exists so the kevinburke.fastagent collection provides a module
named `copy`. With `collections: [kevinburke.fastagent]` set on a play,
unqualified `copy:` tasks resolve to `kevinburke.fastagent.copy`, which
causes Ansible to select our action plugin override at
`plugins/action/copy.py`. The action plugin handles all execution.

This shim is intentionally minimal: it accepts no arguments and fails with
a clear message if invoked directly. The action plugin's fallback paths
explicitly call `_execute_module(module_name="ansible.builtin.copy", ...)`,
so this code path should never run in practice.
"""

from __future__ import annotations

DOCUMENTATION = r"""
---
module: copy
short_description: Routing shim for kevinburke.fastagent copy override
description:
    - Stub module that exists so the C(collections:) play keyword routes
      unqualified C(copy:) tasks to the fastagent action plugin override.
    - All work is performed by the action plugin; this module should never
      be invoked directly.
options: {}
"""

import json
import sys


def main() -> None:
    print(json.dumps({
        "failed": True,
        "msg": (
            "kevinburke.fastagent.copy shim was invoked directly. The action "
            "plugin override should have handled this task. This indicates a "
            "bug in the action plugin's fallback path — fallbacks must pass "
            "module_name='ansible.builtin.copy' to _execute_module()."
        ),
    }))
    sys.exit(1)


if __name__ == "__main__":
    main()
