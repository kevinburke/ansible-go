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
usage: bash .buildkite/ci.sh <format|lint|test|build|python-test>
EOF
}

run_format() {
  differ go fmt ./...
  differ goimports -w .
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
  cd module_utils && python3 -m unittest fastagent_client_test -v
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
  *)
    usage >&2
    exit 1
    ;;
esac
