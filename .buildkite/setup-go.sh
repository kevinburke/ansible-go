#!/usr/bin/env bash
set -euo pipefail

fail() {
  echo "$*" >&2
  return 1 2>/dev/null || exit 1
}

readonly GO_SETUP_SCRIPT_DIR="$(
  CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd
)"
readonly GO_SETUP_REPO_ROOT="$(
  CDPATH= cd -- "${GO_SETUP_SCRIPT_DIR}/.." && pwd
)"

readonly GO_VERSION="${GO_VERSION:?GO_VERSION is required}"
readonly OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
readonly ARCH="$(uname -m)"

# Use a lowercase local var for the host arch — never `GOARCH`. Marking
# GOARCH readonly here would break callers that use the standard
# `GOARCH=amd64 go build ...` syntax to cross-compile, since bash refuses
# even temporary prefix-assignments to readonly variables.
case "${ARCH}" in
  x86_64 | amd64)
    host_goarch="amd64"
    ;;
  aarch64 | arm64)
    host_goarch="arm64"
    ;;
  *)
    fail "unsupported architecture: ${ARCH}"
    ;;
esac

readonly PLATFORM="${OS}-${host_goarch}"
readonly GO_ROOT="${GO_SETUP_REPO_ROOT}/.go/${GO_VERSION}/${PLATFORM}"
readonly GO_BIN="${GO_ROOT}/bin/go"
readonly ARCHIVE_DIR="${GO_SETUP_REPO_ROOT}/.go/cache"
readonly ARCHIVE_PATH="${ARCHIVE_DIR}/go${GO_VERSION}.${PLATFORM}.tar.gz"
readonly TAR_BIN="$(command -v bsdtar || command -v tar)"

if [[ ! -x "${GO_BIN}" ]]; then
  echo "Downloading Go ${GO_VERSION} for ${PLATFORM}..."
  mkdir -p "${ARCHIVE_DIR}"
  curl --fail --silent --show-error --location \
    --output "${ARCHIVE_PATH}" \
    "https://go.dev/dl/go${GO_VERSION}.${PLATFORM}.tar.gz"

  rm -rf "${GO_ROOT}.tmp"
  mkdir -p "${GO_ROOT}.tmp"
  "${TAR_BIN}" -xzf "${ARCHIVE_PATH}" -C "${GO_ROOT}.tmp" --strip-components=1
  rm -rf "${GO_ROOT}"
  mkdir -p "$(dirname -- "${GO_ROOT}")"
  mv "${GO_ROOT}.tmp" "${GO_ROOT}"
else
  echo "Go ${GO_VERSION} already installed in ${GO_ROOT}"
fi

export GOROOT="${GO_ROOT}"
export PATH="${GOROOT}/bin:${PATH}"

installed_version="$("${GO_BIN}" env GOVERSION)"
expected_version="go${GO_VERSION}"
if [[ "${installed_version}" != "${expected_version}" ]]; then
  fail "installed Go version ${installed_version} does not match ${expected_version}"
fi

"${GO_BIN}" version
