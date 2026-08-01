#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPOSITORY_ROOT="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd -P)"
cd -- "${REPOSITORY_ROOT}"

if ! command -v uv >/dev/null 2>&1; then
  USER_DIRECTORY="$(getent passwd "$(id -u)" | cut -d: -f6)"
  PATH="${USER_DIRECTORY}/.local/bin:${PATH}"
  export PATH
fi

exec uv run --project deploy/cmms --frozen --no-dev \
  cmms-development "$@"
