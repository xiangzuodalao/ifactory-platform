#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)"

readonly -a COMPONENTS=(
  "components/cmms"
  "components/thingsboard"
  "components/pdm-algorithm"
  "components/digital-mcp"
  "components/platform-integration"
)

failures=0
warnings=0

pass() {
  printf 'PASS  %s\n' "$*"
}

warn() {
  printf 'WARN  %s\n' "$*" >&2
  warnings=$((warnings + 1))
}

fail() {
  printf 'FAIL  %s\n' "$*" >&2
  failures=$((failures + 1))
}

have_command() {
  local name="$1"
  if command -v "${name}" >/dev/null 2>&1; then
    pass "tool '${name}' is available"
  else
    fail "required tool '${name}' is not available"
  fi
}

validate_skill_metadata() {
  local skill_dir="$1"
  local expected_name="$2"
  local skill_file="${skill_dir}/SKILL.md"
  local metadata_file="${skill_dir}/agents/openai.yaml"

  if [[ ! -f "${skill_file}" ]]; then
    fail "${skill_file#${ROOT_DIR}/} is missing"
    return
  fi

  if [[ "$(sed -n '1p' "${skill_file}")" == "---" ]] &&
    grep -Fqx "name: ${expected_name}" "${skill_file}" &&
    grep -Eq '^description: .+' "${skill_file}"; then
    pass "${expected_name} SKILL.md has required frontmatter"
  else
    fail "${expected_name} SKILL.md is missing valid name/description frontmatter"
  fi

  if [[ -f "${metadata_file}" ]] &&
    grep -Eq '^[[:space:]]*display_name:[[:space:]]*.+' "${metadata_file}" &&
    grep -Eq '^[[:space:]]*short_description:[[:space:]]*.+' "${metadata_file}"; then
    pass "${expected_name} has Codex interface metadata"
  else
    fail "${expected_name} agents/openai.yaml is missing required interface metadata"
  fi
}

have_command bash
have_command git
have_command uv
if command -v codex >/dev/null 2>&1; then
  pass "tool 'codex' is available"
else
  warn "tool 'codex' is not available; it is optional in CI but required for interactive agent use"
fi

if top_level="$(git -C "${ROOT_DIR}" rev-parse --show-toplevel 2>/dev/null)"; then
  if [[ "$(CDPATH= cd -- "${top_level}" && pwd -P)" == "$(CDPATH= cd -- "${ROOT_DIR}" && pwd -P)" ]]; then
    pass "repository root is ${ROOT_DIR}"
  else
    fail "${ROOT_DIR} is nested in another Git worktree"
  fi
else
  fail "${ROOT_DIR} is not a Git worktree"
fi

declare -A declared_names=()
declare -A declared_urls=()

if [[ ! -f "${ROOT_DIR}/.gitmodules" ]]; then
  fail ".gitmodules is missing"
else
  while read -r key component; do
    [[ -n "${key:-}" && -n "${component:-}" ]] || continue
    name="${key#submodule.}"
    name="${name%.path}"
    url="$(git config -f "${ROOT_DIR}/.gitmodules" --get "submodule.${name}.url" 2>/dev/null || true)"
    declared_names["${component}"]="${name}"
    declared_urls["${component}"]="${url}"
  done < <(git config -f "${ROOT_DIR}/.gitmodules" --get-regexp '^submodule\..*\.path$' 2>/dev/null)

  if [[ "${#declared_names[@]}" -eq "${#COMPONENTS[@]}" ]]; then
    pass ".gitmodules declares exactly ${#COMPONENTS[@]} submodules"
  else
    fail ".gitmodules declares ${#declared_names[@]} submodules; expected ${#COMPONENTS[@]}"
  fi
fi

for component in "${COMPONENTS[@]}"; do
  repo_dir="${ROOT_DIR}/${component}"
  url="${declared_urls[${component}]:-}"

  if [[ -z "${declared_names[${component}]:-}" ]]; then
    fail "${component} is not declared in .gitmodules"
    continue
  fi

  if [[ "${url}" != https://* ]] || [[ "${url}" =~ ^https://[^/]*@ ]]; then
    fail "${component} must use an uncredentialed HTTPS submodule URL"
  else
    pass "${component} uses an uncredentialed HTTPS submodule URL"
  fi

  if [[ ! -e "${repo_dir}/.git" ]]; then
    fail "${component} is not initialized"
    continue
  fi

  component_top="$(git -C "${repo_dir}" rev-parse --show-toplevel 2>/dev/null || true)"
  superproject="$(git -C "${repo_dir}" rev-parse --show-superproject-working-tree 2>/dev/null || true)"
  if [[ -n "${component_top}" ]] &&
    [[ "$(CDPATH= cd -- "${component_top}" && pwd -P)" == "$(CDPATH= cd -- "${repo_dir}" && pwd -P)" ]] &&
    [[ -n "${superproject}" ]] &&
    [[ "$(CDPATH= cd -- "${superproject}" && pwd -P)" == "$(CDPATH= cd -- "${ROOT_DIR}" && pwd -P)" ]]; then
    pass "${component} is an initialized submodule"
  else
    fail "${component} is not attached to this superproject as a submodule"
  fi

  origin_url="$(git -C "${repo_dir}" remote get-url origin 2>/dev/null || true)"
  if [[ "${origin_url}" == "${url}" ]]; then
    pass "${component} origin matches .gitmodules"
  else
    fail "${component} origin '${origin_url:-<missing>}' does not match '${url}'"
  fi

  status_line="$(git -C "${ROOT_DIR}" submodule status -- "${component}" 2>/dev/null || true)"
  case "${status_line:0:1}" in
    -) fail "${component} is uninitialized" ;;
    +) warn "${component} is checked out at a commit different from the recorded gitlink" ;;
    U) fail "${component} has an unresolved submodule merge conflict" ;;
    ' ') pass "${component} is at the recorded gitlink" ;;
    *) fail "${component} submodule status could not be determined" ;;
  esac

  if [[ -n "$(git -C "${repo_dir}" status --porcelain 2>/dev/null)" ]]; then
    warn "${component} has local changes"
  else
    pass "${component} worktree is clean"
  fi
done

check_upstream() {
  local component="$1"
  local expected_url="$2"
  local repo_dir="${ROOT_DIR}/${component}"
  local actual_url

  if [[ ! -e "${repo_dir}/.git" ]]; then
    return
  fi

  actual_url="$(git -C "${repo_dir}" remote get-url upstream 2>/dev/null || true)"
  if [[ "${actual_url}" == "${expected_url}" ]]; then
    pass "${component} upstream is correct"
  else
    fail "${component} upstream '${actual_url:-<missing>}' should be '${expected_url}'"
  fi
}

check_upstream "components/cmms" "https://github.com/grashjs/cmms.git"
check_upstream "components/thingsboard" "https://github.com/thingsboard/thingsboard.git"

onboard_link="${ROOT_DIR}/.agents/skills/pdm-onboard-scenario"
expected_onboard_target="../../components/pdm-algorithm/.agents/skills/pdm-onboard-scenario"
if [[ -L "${onboard_link}" ]]; then
  actual_target="$(readlink "${onboard_link}")"
  if [[ "${actual_target}" == "${expected_onboard_target}" ]]; then
    pass "pdm-onboard-scenario uses the expected relative symlink"
  else
    fail "pdm-onboard-scenario symlink target '${actual_target}' is not '${expected_onboard_target}'"
  fi
else
  fail ".agents/skills/pdm-onboard-scenario is not a symlink"
fi

validate_skill_metadata \
  "${ROOT_DIR}/.agents/skills/pdm-train-model" \
  "pdm-train-model"
validate_skill_metadata \
  "${ROOT_DIR}/.agents/skills/pdm-onboard-scenario" \
  "pdm-onboard-scenario"

codex_config="${ROOT_DIR}/.codex/config.toml"
if [[ -f "${codex_config}" ]] &&
  grep -Eq '^\[mcp_servers\.(digital-platform|"digital-platform")\]$' "${codex_config}" &&
  grep -Fqx 'command = "uv"' "${codex_config}" &&
  grep -Fqx 'args = ["run", "--project", "components/digital-mcp", "--frozen", "--no-dev", "pdm-mcp"]' "${codex_config}" &&
  grep -Fqx 'cwd = "."' "${codex_config}"; then
  pass "project Codex config registers digital-platform from the platform root"
else
  fail ".codex/config.toml does not register digital-platform with the expected root-relative command"
fi

mcp_project="${ROOT_DIR}/components/digital-mcp"
if [[ -f "${mcp_project}/pyproject.toml" ]] &&
  [[ -f "${mcp_project}/uv.lock" ]] &&
  grep -Eq '^pdm-mcp[[:space:]]*=' "${mcp_project}/pyproject.toml"; then
  pass "digital-mcp has a frozen lockfile and the pdm-mcp entry point"
else
  fail "digital-mcp is missing pyproject.toml, uv.lock, or the pdm-mcp entry point"
fi

if [[ -n "$(git -C "${ROOT_DIR}" status --porcelain 2>/dev/null)" ]]; then
  warn "platform worktree has local changes"
else
  pass "platform worktree is clean"
fi

printf '\nDoctor completed with %d failure(s) and %d warning(s).\n' "${failures}" "${warnings}"
if ((failures > 0)); then
  exit 1
fi
