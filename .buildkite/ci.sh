#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_DIR="$(
  CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd
)"
readonly REPO_ROOT="$(
  CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd
)"

cd "${REPO_ROOT}"

export GO_VERSION="${GO_VERSION:-1.26.2}"
export STATICCHECK_VERSION="${STATICCHECK_VERSION:-v0.7.0}"
export DIFFER_VERSION="${DIFFER_VERSION:-v0.0.0-20260403230520-c0574ebcacb2}"
export ANSIBLE_VERSION="${ANSIBLE_VERSION:-2.18.5}"
export GOTOOLCHAIN=local

goflags="${GOFLAGS:-}"
if [[ -n "${goflags}" ]]; then
  goflags+=" "
fi
goflags+="-trimpath"
export GOFLAGS="${goflags}"

source "${SCRIPT_DIR}/setup-go.sh"
source "${SCRIPT_DIR}/setup-tools.sh"

usage() {
  cat <<'EOF'
usage: bash .buildkite/ci.sh <format|lint|test|build|python-test|check-versions>
EOF
}

run_format() {
  # `./...` is module-aware and excludes the downloaded Go SDK in .go/.
  differ go fmt ./...
  # `goimports -w .` would naively walk every directory under the repo
  # root, including the downloaded Go SDK in .go/, and choke on the SDK's
  # intentional-syntax-error files under test/syntax/. Restrict to tracked
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

  # 3. Collection layout tests: build the tarball, install it to a temp
  #    ANSIBLE_COLLECTIONS_PATH, and verify ansible-doc + module_utils
  #    imports resolve through the installed collection.
  (cd "${REPO_ROOT}" && \
    python3 -m unittest tests.test_collection_layout -v)
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
