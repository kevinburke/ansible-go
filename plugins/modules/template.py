#!/usr/bin/python
"""Routing-only shim for the template module.

This file exists so the kevinburke.fastagent collection provides a module
named `template`. With `collections: [kevinburke.fastagent]` set on a play,
unqualified `template:` tasks resolve to `kevinburke.fastagent.template`, which
causes Ansible to select our action plugin override at
`plugins/action/template.py`. The action plugin handles all execution.

This shim is intentionally minimal: it accepts no arguments and fails with
a clear message if invoked directly. The action plugin renders on the
controller and hands the result to the copy action, as ansible-core's
template action does; nothing runs a module named `template`, so this code
path should never run in practice.
"""

from __future__ import annotations

DOCUMENTATION = r"""
---
module: template
short_description: Routing shim for kevinburke.fastagent template override
description:
    - Stub module that exists so the C(collections:) play keyword routes
      unqualified C(template:) tasks to the fastagent action plugin override.
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
            "kevinburke.fastagent.template shim was invoked directly. The "
            "action plugin override should have handled this task: it "
            "renders the template and hands the result to the copy action, "
            "and never runs a module named template."
        ),
    }))
    sys.exit(1)


if __name__ == "__main__":
    main()
