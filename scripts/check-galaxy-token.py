#!/usr/bin/env python3
"""Check local Galaxy credentials before a release creates tags or uploads."""

import argparse
import os
from pathlib import Path

from ansible import constants as C
import yaml


def check_token():
    token = os.environ.get('ANSIBLE_GALAXY_TOKEN')
    if not token:
        path = Path(C.GALAXY_TOKEN_PATH)
        try:
            config = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError):
            # YAML parser diagnostics can include the credential itself.
            raise SystemExit(f'Cannot read a Galaxy token mapping from {path}') from None
        token = config.get('token') if isinstance(config, dict) else None
    if not isinstance(token, str) or not token or token.strip() != token:
        raise SystemExit('Galaxy publish requires a non-empty token without surrounding whitespace; '
                         'set ANSIBLE_GALAXY_TOKEN or token: in the configured Galaxy token file')


if __name__ == '__main__':
    argparse.ArgumentParser(description=__doc__).parse_args()
    check_token()
