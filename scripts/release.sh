#!/usr/bin/env bash
#
# release.sh — cut a new fastagent release.
#
# Reads the canonical version from fastagent.go, verifies that every other
# version reference in the repo matches, runs the full test suite, builds
# the linux binaries and the Ansible collection tarball, tags the commit,
# pushes the tag, creates a GitHub release with the binaries attached, and
# (unless --skip-galaxy is passed) publishes the collection to Ansible
# Galaxy.
#
# Usage:
#   scripts/release.sh              # full release
#   scripts/release.sh --dry-run    # print every action, do nothing
#   scripts/release.sh --skip-galaxy
#
# Requirements on the host running this script:
#   - go, make, python3 (for tests)
#   - ansible-galaxy (for collection build + publish)
#   - gh (GitHub CLI, authenticated: `gh auth status`)
#
# Required environment for Galaxy publish (omit with --skip-galaxy):
#   - ANSIBLE_GALAXY_TOKEN  (or ~/.ansible/galaxy_token)
#

set -euo pipefail

# ---- argument parsing ------------------------------------------------------

DRY_RUN=0
SKIP_GALAXY=0

for arg in "$@"; do
    case "$arg" in
        --dry-run)     DRY_RUN=1 ;;
        --skip-galaxy) SKIP_GALAXY=1 ;;
        -h|--help)
            sed -n '2,/^$/p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "release.sh: unknown argument '$arg'" >&2
            echo "usage: $0 [--dry-run] [--skip-galaxy]" >&2
            exit 2
            ;;
    esac
done

# ---- small helpers ---------------------------------------------------------

log() { printf '==> %s\n' "$*"; }
err() { printf '!!  %s\n' "$*" >&2; }

run() {
    # Echo the command, then run it — unless --dry-run.
    printf '    $ %s\n' "$*"
    if [ "$DRY_RUN" -eq 0 ]; then
        "$@"
    fi
}

require_tool() {
    if ! command -v "$1" >/dev/null 2>&1; then
        err "required tool not found on PATH: $1"
        exit 1
    fi
}

# ---- locate the repo root (script must run from anywhere) -----------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# ---- preflight: tooling ----------------------------------------------------

log "checking required tools"
require_tool go
require_tool make
require_tool python3
require_tool ansible-galaxy
require_tool gh
require_tool git

# ---- preflight: git state --------------------------------------------------

log "verifying git state"

if ! git diff-index --quiet HEAD -- ; then
    err "working tree has uncommitted changes. Commit or stash first."
    git status --short >&2
    exit 1
fi

current_branch="$(git rev-parse --abbrev-ref HEAD)"
if [ "$current_branch" != "main" ]; then
    err "must release from 'main' branch, currently on '$current_branch'"
    exit 1
fi

run git fetch origin main
local_head="$(git rev-parse HEAD)"
origin_head="$(git rev-parse origin/main)"
if [ "$local_head" != "$origin_head" ]; then
    err "local main ($local_head) is not in sync with origin/main ($origin_head)"
    err "pull or push to align, then retry."
    exit 1
fi

# ---- version: canonical source is fastagent.go ----------------------------
#
# Delegate to scripts/check-versions.sh so the same consistency check runs
# in CI on every push and at release time. If any of fastagent.go,
# galaxy.yml, or plugins/connection/fastagent.py disagree on the version,
# this exits with a clear error.

log "verifying all version references agree"
VERSION=$("$SCRIPT_DIR/check-versions.sh" --print)
log "canonical version: $VERSION"

# ---- preflight: tag must not already exist --------------------------------

TAG="v$VERSION"
if git rev-parse --verify --quiet "refs/tags/$TAG" >/dev/null; then
    err "tag $TAG already exists locally. Either bump the version or delete the tag."
    exit 1
fi
if git ls-remote --exit-code --tags origin "refs/tags/$TAG" >/dev/null 2>&1; then
    err "tag $TAG already exists on origin. Bump the version and retry."
    exit 1
fi

# ---- preflight: Galaxy token (if publishing) ------------------------------

if [ "$SKIP_GALAXY" -eq 0 ]; then
    if [ -z "${ANSIBLE_GALAXY_TOKEN:-}" ] && \
       [ ! -f "$HOME/.ansible/galaxy_token" ]; then
        err "Galaxy publish requested but no token found."
        err "Set ANSIBLE_GALAXY_TOKEN or create ~/.ansible/galaxy_token,"
        err "or rerun with --skip-galaxy."
        exit 1
    fi
fi

# ---- build & test ----------------------------------------------------------

log "running full test suite and building release artifacts"
run make clean
run make release

# Sanity-check that the artifacts we'll upload actually exist.
TARBALL="tmp/kevinburke-fastagent-${VERSION}.tar.gz"
AMD64_BIN="tmp/fastagent-linux-amd64"
ARM64_BIN="tmp/fastagent-linux-arm64"
for artifact in "$TARBALL" "$AMD64_BIN" "$ARM64_BIN"; do
    if [ "$DRY_RUN" -eq 0 ] && [ ! -f "$artifact" ]; then
        err "expected release artifact missing: $artifact"
        exit 1
    fi
done

# ---- tag + push ------------------------------------------------------------

log "tagging $TAG"
run git tag -a "$TAG" -m "fastagent $TAG"
run git push origin "$TAG"

# The GitHub CLI needs the tag on GitHub, not just on the local origin.
# Push both the branch and tag to the 'github' remote if it exists.
if git remote get-url github >/dev/null 2>&1; then
    log "pushing to github remote"
    run git push github main "$TAG"
fi

# ---- GitHub release --------------------------------------------------------

log "creating GitHub release $TAG"
# Rename the binaries on upload so the release assets match the connection
# plugin's download_url template: fastagent-{version}-linux-{arch}
AMD64_ASSET="tmp/fastagent-${VERSION}-linux-amd64"
ARM64_ASSET="tmp/fastagent-${VERSION}-linux-arm64"
if [ "$DRY_RUN" -eq 0 ]; then
    cp "$AMD64_BIN" "$AMD64_ASSET"
    cp "$ARM64_BIN" "$ARM64_ASSET"
fi

run gh release create "$TAG" \
    "$AMD64_ASSET" \
    "$ARM64_ASSET" \
    --title "$TAG" \
    --generate-notes

# ---- Ansible Galaxy publish ------------------------------------------------

if [ "$SKIP_GALAXY" -eq 1 ]; then
    log "--skip-galaxy set, not publishing to Ansible Galaxy"
else
    log "publishing collection to Ansible Galaxy"
    if [ -n "${ANSIBLE_GALAXY_TOKEN:-}" ]; then
        run ansible-galaxy collection publish "$TARBALL" \
            --token "$ANSIBLE_GALAXY_TOKEN"
    else
        # Token comes from ~/.ansible/galaxy_token.
        run ansible-galaxy collection publish "$TARBALL"
    fi
fi

log "release $TAG complete"
if [ "$DRY_RUN" -eq 1 ]; then
    log "(dry run: nothing was actually changed)"
fi
