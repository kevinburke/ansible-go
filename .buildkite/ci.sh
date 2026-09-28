#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_DIR="$(
  CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd
)"
readonly REPO_ROOT="$(
  CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd
)"

cd "${REPO_ROOT}"

: "${STATICCHECK_VERSION:?STATICCHECK_VERSION is required}"
: "${GOIMPORTS_VERSION:?GOIMPORTS_VERSION is required}"
: "${DIFFER_VERSION:?DIFFER_VERSION is required}"
: "${ANSIBLE_VERSION:?ANSIBLE_VERSION is required}"
export STATICCHECK_VERSION GOIMPORTS_VERSION DIFFER_VERSION ANSIBLE_VERSION
# Use the Go toolchain the Buildkite agent provides on PATH (host-managed,
# /usr/local/go on the agents) rather than downloading one per job.
# GOTOOLCHAIN=local turns an agent whose Go is older than the `go` line in
# go.mod into a hard error instead of a silent toolchain download.
export GOTOOLCHAIN=local
if ! command -v go >/dev/null 2>&1; then
  echo "go not found on PATH; the Buildkite agent must provide a Go toolchain" >&2
  exit 1
fi

goflags="${GOFLAGS:-}"
if [[ -n "${goflags}" ]]; then
  goflags+=" "
fi
goflags+="-trimpath"
export GOFLAGS="${goflags}"

go version
source "${SCRIPT_DIR}/setup-tools.sh"

usage() {
  cat <<'EOF'
usage: bash .buildkite/ci.sh <format|lint|test|build|python-test|check-versions>
EOF
}

run_format() {
  differ go fmt ./...
  # `goimports -w .` would walk every directory under the repo root,
  # including untracked ones like tmp/ and worktrees/. Restrict to tracked
  # files only.
  differ sh -c "git ls-files -z -- '*.go' | xargs -0 goimports -w"
}

run_lint() {
  go vet ./...
  staticcheck ./...
}

run_test() {
  go test -race -cover ./...
}

run_build() {
  local build_dir
  mkdir -p "${REPO_ROOT}/tmp"
  build_dir="$(mktemp -d "${REPO_ROOT}/tmp/buildkite-ci.XXXXXX")"
  trap "rm -rf '${build_dir}'" EXIT

  GOOS=linux GOARCH=amd64 go build -o "${build_dir}/fastagent-linux-amd64" ./cmd/fastagent
  GOOS=linux GOARCH=arm64 go build -o "${build_dir}/fastagent-linux-arm64" ./cmd/fastagent
}

run_python_test() {
  # Provision a venv with ansible-core so the collection layout test (which
  # invokes ansible-galaxy and ansible-doc) can run. setup-python.sh exports
  # PATH so the venv's python3 / ansible-galaxy come first.
  source "${SCRIPT_DIR}/setup-python.sh"

  # 1. JSON-RPC client integration tests: build the Go agent and round-trip
  #    real RPCs through the Python client.
  (cd "${REPO_ROOT}/plugins/module_utils" && \
    python3 -m unittest fastagent_client_test -v)

  # 2. Connection plugin regression tests (socket timeout, etc.).
  (cd "${REPO_ROOT}/plugins/connection" && \
    python3 -m unittest fastagent_test -v)

  # 3. Everything under tests/ (collection layout, action-plugin helpers,
  #    etc.). Discover picks up any new tests/test_*.py automatically so a
  #    future test file can't be silently skipped by CI.
  (cd "${REPO_ROOT}" && \
    python3 -m unittest discover -v -s tests -t . -p 'test_*.py')
}

run_check_versions() {
  bash "${REPO_ROOT}/scripts/check-versions.sh"
}

if [[ $# -ne 1 ]]; then
  usage >&2
  exit 1
fi

case "$1" in
  format)
    run_format
    ;;
  lint)
    run_lint
    ;;
  test)
    run_test
    ;;
  build)
    run_build
    ;;
  python-test)
    run_python_test
    ;;
  check-versions)
    run_check_versions
    ;;
  *)
    usage >&2
    exit 1
    ;;
esac
