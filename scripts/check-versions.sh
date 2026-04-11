#!/usr/bin/env bash
#
# check-versions.sh — verify that every version reference in the repo
# matches the canonical version in fastagent.go.
#
# Three places carry the agent version: fastagent.go (Go const),
# galaxy.yml (collection metadata), and plugins/connection/fastagent.py
# (AGENT_VERSION constant the connection plugin uses to validate the
# remote daemon). Forgetting to bump one of them is a release-time
# footgun: the connection plugin would compare a stale version against
# the actual deployed daemon and either trip "version mismatch" forever
# or silently accept a wrong binary.
#
# This script catches the mismatch *before* a release. It's safe to run
# from anywhere (no git state assumptions) and is invoked both by
# scripts/release.sh and from CI on every push.
#
# Usage:
#   scripts/check-versions.sh                       # exits non-zero on mismatch
#   VERSION=$(scripts/check-versions.sh --print)    # also prints the version

set -euo pipefail

PRINT=0
if [[ "${1:-}" == "--print" ]]; then
    PRINT=1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# Canonical source: fastagent.go's `const Version = "X.Y.Z"`.
go_version=$(awk -F'"' '/^const Version/ { print $2 }' fastagent.go)
if [[ -z "$go_version" ]]; then
    echo "check-versions: could not extract Version from fastagent.go" >&2
    exit 1
fi

galaxy_version=$(awk '/^version:/ { print $2 }' galaxy.yml)
plugin_version=$(awk -F'"' '/^AGENT_VERSION/ { print $2 }' \
    plugins/connection/fastagent.py)

failed=0
if [[ "$galaxy_version" != "$go_version" ]]; then
    echo "check-versions: galaxy.yml version '$galaxy_version' != fastagent.go '$go_version'" >&2
    failed=1
fi
if [[ "$plugin_version" != "$go_version" ]]; then
    echo "check-versions: plugins/connection/fastagent.py AGENT_VERSION '$plugin_version' != fastagent.go '$go_version'" >&2
    failed=1
fi

if [[ "$failed" -ne 0 ]]; then
    echo "check-versions: bump every reference to $go_version and retry." >&2
    exit 1
fi

if [[ "$PRINT" -eq 1 ]]; then
    printf '%s\n' "$go_version"
fi
