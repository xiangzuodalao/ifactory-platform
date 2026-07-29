#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)"

readonly -a COMPONENTS=(
  "components/cmms"
  "components/thingsboard"
  "components/pdm-algorithm"
  "components/digital-mcp"
  "components/platform-integration"
)

die() {
  printf 'bootstrap: ERROR: %s\n' "$*" >&2
  exit 1
}

note() {
  printf 'bootstrap: %s\n' "$*"
}

command -v git >/dev/null 2>&1 || die "git is required"
[[ -f "${ROOT_DIR}/.gitmodules" ]] || die "missing ${ROOT_DIR}/.gitmodules"

top_level="$(git -C "${ROOT_DIR}" rev-parse --show-toplevel 2>/dev/null)" ||
  die "${ROOT_DIR} is not a Git worktree"
[[ "$(CDPATH= cd -- "${top_level}" && pwd -P)" == "$(CDPATH= cd -- "${ROOT_DIR}" && pwd -P)" ]] ||
  die "run this script from the ifactory-platform worktree"

for component in "${COMPONENTS[@]}"; do
  if ! git config -f "${ROOT_DIR}/.gitmodules" --get-regexp '^submodule\..*\.path$' |
    awk -v expected="${component}" '$2 == expected { found = 1 } END { exit !found }'; then
    die "${component} is not declared in .gitmodules"
  fi
done

note "syncing submodule URLs"
git -C "${ROOT_DIR}" submodule sync --recursive

for component in "${COMPONENTS[@]}"; do
  if [[ -e "${ROOT_DIR}/${component}/.git" ]]; then
    superproject="$(
      git -C "${ROOT_DIR}/${component}" rev-parse --show-superproject-working-tree 2>/dev/null ||
        true
    )"
    if [[ -z "${superproject}" ]] ||
      [[ "$(CDPATH= cd -- "${superproject}" && pwd -P)" != "$(CDPATH= cd -- "${ROOT_DIR}" && pwd -P)" ]]; then
      die "${component} exists but is not attached to this superproject as a submodule"
    fi
    note "${component} is already initialized; leaving its branch and commit unchanged"
    continue
  fi

  note "initializing ${component}"
  git -C "${ROOT_DIR}" submodule update --init --recursive -- "${component}"
done

ensure_remote() {
  local component="$1"
  local remote_name="$2"
  local expected_url="$3"
  local repo_dir="${ROOT_DIR}/${component}"
  local current_url

  [[ -e "${repo_dir}/.git" ]] || die "${component} is not an initialized Git submodule"

  if current_url="$(git -C "${repo_dir}" remote get-url "${remote_name}" 2>/dev/null)"; then
    if [[ "${current_url}" != "${expected_url}" ]]; then
      die "${component} remote '${remote_name}' is ${current_url}; expected ${expected_url}"
    fi
    note "${component} remote '${remote_name}' is already configured"
    return
  fi

  git -C "${repo_dir}" remote add "${remote_name}" "${expected_url}"
  note "added ${component} remote '${remote_name}' -> ${expected_url}"
}

ensure_remote \
  "components/cmms" \
  "upstream" \
  "https://github.com/grashjs/cmms.git"
ensure_remote \
  "components/thingsboard" \
  "upstream" \
  "https://github.com/thingsboard/thingsboard.git"

note "complete; no component branch was switched and no dependencies were installed"
