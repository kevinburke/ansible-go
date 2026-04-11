#!/usr/bin/env bash
#
# setup-python.sh — provisions a venv with the pinned ansible-core version.
#
# Mirrors setup-tools.sh: keeps the venv on disk under .python-venv and uses
# a stamp file so the install only happens once per ANSIBLE_VERSION value.
# Sourcing this script puts the venv's bin dir on PATH for the caller.

set -euo pipefail

if [[ -z "${REPO_ROOT:-}" ]]; then
  readonly REPO_ROOT="$(
    CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd
  )"
fi

readonly ANSIBLE_VERSION="${ANSIBLE_VERSION:?ANSIBLE_VERSION is required}"
readonly PYTHON_VENV="${REPO_ROOT}/.python-venv"
readonly PYTHON_STAMP="${PYTHON_VENV}/.ansible-core-${ANSIBLE_VERSION}.stamp"

if [[ ! -f "${PYTHON_STAMP}" ]]; then
  rm -rf "${PYTHON_VENV}"
  python3 -m venv "${PYTHON_VENV}"
  "${PYTHON_VENV}/bin/pip" install --quiet --upgrade pip
  "${PYTHON_VENV}/bin/pip" install --quiet "ansible-core==${ANSIBLE_VERSION}"
  : > "${PYTHON_STAMP}"
fi

export PATH="${PYTHON_VENV}/bin:${PATH}"
