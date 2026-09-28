#!/bin/sh
# dev-install.sh: build this checkout, including uncommitted and untracked
# files, as a uniquely versioned development release, and install it where
# only commands that opt in will use it.
#
# Usage:
#   scripts/dev-install.sh            (or: make dev-install)
#   ~/.ansible/fastagent-dev/run ansible-playbook -i inventory site.yml
#
# Why a new version for every build: the version names the agent binary on
# the controller and the remote host, the remote daemon's socket, and what
# the Hello handshake checks. A development build that kept the release
# version would be skipped on any host that already has that release's
# agent, and would be uploaded to hosts that don't by real deploys reading
# the same agent cache. A dev version (for example 0.9.0-dev.g39d5d02abcde,
# plus .w<tree hash> when the working tree has changes) can't collide with a
# release, and the connection plugin warns that it is a development build.
#
# What it writes:
#   $FASTAGENT_DEV_AGENT_DIR (default ~/.ansible/fastagent)
#       fastagent-<dev version>-linux-<arch>, next to the release binaries,
#       where the connection plugin looks first. The names never match a
#       release, so release deploys never read them.
#   $FASTAGENT_DEV_DEST (default ~/.ansible/fastagent-dev)
#       collections/  the collection, stamped with the dev version. Nothing
#                     uses it unless ANSIBLE_COLLECTIONS_PATH points there.
#       env.sh        exports ANSIBLE_COLLECTIONS_PATH (this directory first,
#                     then the usual paths, for other collections) and
#                     ANSIBLE_ACTION_PLUGINS (the dev collection's action
#                     plugins, overriding an ansible.cfg action_plugins).
#       run           runs a command with env.sh applied.
#
# Other settings: FASTAGENT_DEV_ARCHES (default "amd64 arm64"), GO (default
# go), ANSIBLE_GALAXY (default ansible-galaxy).
set -eu

die() {
    echo "dev-install: $*" >&2
    exit 1
}

case "${1:-}" in
-h | --help)
    sed -n '2,/^set -eu/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
    exit 0
    ;;
"") ;;
*) die "unexpected argument $1 (see --help)" ;;
esac

REPO=$(cd "$(dirname "$0")/.." && pwd)
DEST=${FASTAGENT_DEV_DEST:-$HOME/.ansible/fastagent-dev}
AGENT_DIR=${FASTAGENT_DEV_AGENT_DIR:-$HOME/.ansible/fastagent}
ARCHES=${FASTAGENT_DEV_ARCHES:-amd64 arm64}
GO=${GO:-go}
GALAXY=${ANSIBLE_GALAXY:-ansible-galaxy}

command -v "$GO" >/dev/null || die "$GO not found"
command -v "$GALAXY" >/dev/null || die "$GALAXY not found; activate a venv with ansible-core"
cd "$REPO"

base=$(sed -n 's/^const Version = "\(.*\)"$/\1/p' fastagent.go)
[ -n "$base" ] || die "could not read const Version from fastagent.go"

# Snapshot the working tree, untracked files included and ignored files
# excluded, through a temporary index so the real one is untouched.
commit=$(git rev-parse --short=12 HEAD)
index=$(mktemp "${TMPDIR:-/tmp}/fastagent-dev-index.XXXXXX")
trap 'rm -f "$index"' EXIT
GIT_INDEX_FILE=$index git read-tree HEAD
GIT_INDEX_FILE=$index git add -A
tree=$(GIT_INDEX_FILE=$index git write-tree)
# The g and w prefixes keep each identifier alphanumeric: semver, which
# ansible-galaxy enforces, rejects numeric identifiers with leading zeros.
version="$base-dev.g$commit"
if [ "$tree" != "$(git rev-parse 'HEAD^{tree}')" ]; then
    version="$version.w$(echo "$tree" | cut -c1-10)"
fi

out=$REPO/tmp/dev/$version
stage=$out/src
rm -rf "$out"
mkdir -p "$stage"
# Not a pipe: plain sh has no pipefail, so a failed archive would go unseen.
git archive -o "$out/src.tar" "$tree"
tar -x -C "$stage" -f "$out/src.tar"

# Stamp the version everywhere it is defined, and check each edit landed.
stamp() { # file, line before, line after
    old=$2 new=$3
    [ "$(grep -cxF -- "$old" "$stage/$1")" = 1 ] || die "$1: expected exactly one line: $old"
    python3 - "$stage/$1" "$old" "$new" <<'EOF'
import sys
path, old, new = sys.argv[1:]
with open(path, encoding="utf-8") as f:
    lines = f.read().split("\n")
with open(path, "w", encoding="utf-8") as f:
    f.write("\n".join(new if line == old else line for line in lines))
EOF
    [ "$(grep -cxF -- "$new" "$stage/$1")" = 1 ] || die "$1: version not stamped"
}
stamp fastagent.go "const Version = \"$base\"" "const Version = \"$version\""
stamp plugins/connection/fastagent.py "AGENT_VERSION = \"$base\"" "AGENT_VERSION = \"$version\""
stamp galaxy.yml "version: $base" "version: $version"

mkdir -p "$AGENT_DIR"
for arch in $ARCHES; do
    bin=fastagent-$version-linux-$arch
    (cd "$stage" && CGO_ENABLED=0 GOOS=linux GOARCH=$arch "$GO" build -trimpath -o "$out/$bin" ./cmd/fastagent)
    # The binary prints this string for --version; make sure it is there.
    grep -qF "$version" "$out/$bin" || die "$bin does not contain version $version"
    cp "$out/$bin" "$AGENT_DIR/$bin.tmp"
    chmod 755 "$AGENT_DIR/$bin.tmp"
    mv "$AGENT_DIR/$bin.tmp" "$AGENT_DIR/$bin"
done

# ansible-galaxy's output goes to a log, shown on failure: it refuses to
# run when stdout or stderr is a non-blocking handle, which some CI and
# editor terminals hand out.
galaxy() { # log name, arguments...
    log=$out/$1.log
    shift
    "$GALAXY" "$@" </dev/null >"$log" 2>&1 || {
        cat "$log" >&2
        die "ansible-galaxy $* failed"
    }
}
galaxy build collection build --force --output-path "$out" "$stage"
tarball=$out/kevinburke-fastagent-$version.tar.gz
[ -f "$tarball" ] || die "ansible-galaxy did not write $tarball"
galaxy install collection install --offline --force -p "$DEST/collections" "$tarball"
installed=$DEST/collections/ansible_collections/kevinburke/fastagent
grep -qxF "AGENT_VERSION = \"$version\"" "$installed/plugins/connection/fastagent.py" ||
    die "$installed does not hold version $version"

# Ansible's default collection paths come after the dev one, so other
# collections still resolve.
paths=${ANSIBLE_COLLECTIONS_PATH:-$HOME/.ansible/collections:/usr/share/ansible/collections}
cat >"$DEST/env.sh.tmp" <<EOF
# Written by $REPO/scripts/dev-install.sh for fastagent $version.
export ANSIBLE_COLLECTIONS_PATH='$DEST/collections:$paths'
export ANSIBLE_ACTION_PLUGINS='$installed/plugins/action'
EOF
mv "$DEST/env.sh.tmp" "$DEST/env.sh"
cat >"$DEST/run.tmp" <<EOF
#!/bin/sh
# Run a command against fastagent $version (see env.sh).
. '$DEST/env.sh'
exec "\$@"
EOF
chmod 755 "$DEST/run.tmp"
mv "$DEST/run.tmp" "$DEST/run"

echo "fastagent $version"
echo "  agents:     $AGENT_DIR/fastagent-$version-linux-{$(printf %s "$ARCHES" | tr -s ' ' ,)}"
echo "  collection: $installed"
echo "Run a playbook against it with:"
echo "  $DEST/run ansible-playbook ..."
