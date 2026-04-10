#!/usr/bin/env bash
set -euo pipefail

if [[ -z "${REPO_ROOT:-}" ]]; then
  readonly REPO_ROOT="$(
    CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd
  )"
fi

export GOBIN="${REPO_ROOT}/.tools/bin"
readonly TOOL_STAMP_DIR="${REPO_ROOT}/.tools/stamps"

mkdir -p "${GOBIN}" "${TOOL_STAMP_DIR}"
export PATH="${GOBIN}:${PATH}"

install_tool() {
  local binary="$1"
  local pkg="$2"
  local version="$3"
  local stamp_file="${TOOL_STAMP_DIR}/${binary}-${version}"

  if [[ -x "${GOBIN}/${binary}" && -f "${stamp_file}" ]]; then
    return
  fi

  find "${TOOL_STAMP_DIR}" -maxdepth 1 -type f -name "${binary}-*" -delete
  GOFLAGS="" GOBIN="${GOBIN}" go install "${pkg}@${version}"
  : > "${stamp_file}"
}

install_tool differ github.com/kevinburke/differ "${DIFFER_VERSION:?DIFFER_VERSION is required}"
install_tool staticcheck honnef.co/go/tools/cmd/staticcheck "${STATICCHECK_VERSION:?STATICCHECK_VERSION is required}"
