#!/usr/bin/env bash
# bench.sh: time a converged 250-task playbook over plain SSH (pipelining on)
# and over fastagent with the action-plugin overrides, against one host.
#
# Usage: scripts/bench/bench.sh HOST [RUNS]
#
# HOST is an SSH alias or name that needs no extra flags: the user, key and
# port come from ~/.ssh/config. The login user needs passwordless sudo and
# /usr/bin/python3. RUNS (default 5) timed runs of each variant alternate
# so drift in host load hits both equally; each variant gets one untimed
# warm-up run first (for fastagent that run uploads the agent and starts the
# daemon).
#
# The checkout is built with scripts/dev-install.sh, so the numbers describe
# the working tree, not a released version. Everything the benchmark creates
# on the host lives in /tmp/fastagent-bench and is removed on exit.
set -euo pipefail

usage() {
    sed -n '2,/^set -euo/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
}

case "${1:-}" in
-h | --help)
    usage
    exit 0
    ;;
"")
    usage >&2
    exit 2
    ;;
esac
host=$1
runs=${2:-5}
case $runs in
'' | *[!0-9]* | 0)
    echo "bench: RUNS must be a positive integer, got '$runs'" >&2
    exit 2
    ;;
esac

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
work=$(mktemp -d "${TMPDIR:-/tmp}/fastagent-bench.XXXXXX")
dev=${FASTAGENT_DEV_DEST:-$HOME/.ansible/fastagent-dev}

python3 -I "$here/gen_playbook.py" "$work" 250

cat >"$work/ansible.cfg" <<CFG
[defaults]
retry_files_enabled = False
nocows = True
host_key_checking = True

[ssh_connection]
pipelining = True
ssh_args = -o ControlMaster=auto -o ControlPersist=60s
control_path_dir = $work/cp
CFG
mkdir -p "$work/cp"

# fastagent starts its daemon with --allow-user only when Ansible knows the
# remote user, and a daemon started without it resets every connection. The
# user normally comes from ~/.ssh/config, so ask ssh and pass it explicitly
# to both variants.
user=$(ssh -G "$host" | awk '$1 == "user" { print $2; exit }')
[ -n "$user" ] || {
    echo "bench: could not determine the SSH user for $host" >&2
    exit 1
}

for variant in ssh fastagent; do
    conn=ssh
    [ "$variant" = fastagent ] && conn=kevinburke.fastagent.fastagent
    cat >"$work/inventory-$variant" <<INV
[bench]
$host ansible_user=$user ansible_python_interpreter=/usr/bin/python3 ansible_become=true ansible_connection=$conn
INV
done

cleanup() {
    status=$?
    # Teardown must not mask the benchmark's own exit status, but a failed
    # teardown has to be visible: it leaves files on the host.
    if ! ANSIBLE_CONFIG=$work/ansible.cfg ansible-playbook \
        --inventory "$work/inventory-ssh" "$work/teardown.yml" \
        >"$work/teardown.log" 2>&1; then
        echo "bench: teardown failed; /tmp/fastagent-bench may remain on $host" >&2
        tail -n 20 "$work/teardown.log" >&2
        [ "$status" -eq 0 ] && status=1
    fi
    echo "bench: logs in $work" >&2
    exit "$status"
}
trap cleanup EXIT

echo "bench: building $repo with dev-install" >&2
"$repo/scripts/dev-install.sh" >"$work/dev-install.log" 2>&1 || {
    cat "$work/dev-install.log" >&2
    exit 1
}
version=$(sed -n 's/^fastagent //p' "$work/dev-install.log")
[ -n "$version" ] || {
    echo "bench: could not read the dev version from dev-install output" >&2
    exit 1
}

# run VARIANT LOG: run the converged playbook; require a clean, fully "ok"
# recap so a failed or non-converged run cannot be mistaken for a fast one.
run() {
    local variant=$1 log=$2 start end
    local -a prefix=()
    [ "$variant" = fastagent ] && prefix=("$dev/run")
    start=$(date +%s.%N)
    ANSIBLE_CONFIG=$work/ansible.cfg "${prefix[@]}" ansible-playbook \
        --inventory "$work/inventory-$variant" "$work/bench.yml" >"$log" 2>&1 || {
        tail -n 40 "$log" >&2
        echo "bench: $variant run failed (log: $log)" >&2
        return 1
    }
    end=$(date +%s.%N)
    grep -Eq "ok=250 +changed=0 +unreachable=0 +failed=0 +skipped=0 " "$log" || {
        grep -E "ok=" "$log" >&2 || true
        echo "bench: $variant run was not 250 ok / 0 changed (log: $log)" >&2
        return 1
    }
    awk -v s="$start" -v e="$end" 'BEGIN { printf "%.2f\n", e - s }'
}

echo "bench: converging $host" >&2
ANSIBLE_CONFIG=$work/ansible.cfg ansible-playbook \
    --inventory "$work/inventory-ssh" "$work/setup.yml" >"$work/setup.log" 2>&1 || {
    tail -n 40 "$work/setup.log" >&2
    exit 1
}
# Second pass so the timed playbooks start from a truly converged host.
ANSIBLE_CONFIG=$work/ansible.cfg ansible-playbook \
    --inventory "$work/inventory-ssh" "$work/setup.yml" >"$work/setup2.log" 2>&1

for variant in ssh fastagent; do
    echo "bench: warm-up $variant" >&2
    run "$variant" "$work/warmup-$variant.log" >/dev/null
done

ssh_times=()
fa_times=()
for i in $(seq 1 "$runs"); do
    t=$(run ssh "$work/ssh-$i.log")
    ssh_times+=("$t")
    echo "bench: ssh $i/$runs: ${t}s" >&2
    t=$(run fastagent "$work/fastagent-$i.log")
    fa_times+=("$t")
    echo "bench: fastagent $i/$runs: ${t}s" >&2
done

summarize() { # label, times...
    local label=$1
    shift
    printf '%s\n' "$@" | sort -n | awk -v label="$label" '
        { a[NR] = $1 }
        END {
            m = (NR % 2) ? a[(NR + 1) / 2] : (a[NR / 2] + a[NR / 2 + 1]) / 2
            printf "%-10s min %6.2fs  median %6.2fs  max %6.2fs  (n=%d)\n", label, a[1], m, a[NR], NR
        }'
}
echo
echo "host $host, fastagent $version, ansible $(ansible --version | head -n 1)"
summarize ssh "${ssh_times[@]}"
summarize fastagent "${fa_times[@]}"
