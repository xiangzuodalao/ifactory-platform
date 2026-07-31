#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

confirm_path=""
confirm_corrupt=0
while (($#)); do
    case "$1" in
        --confirm-path)
            (($# >= 2)) || { echo "--confirm-path requires a value" >&2; exit 2; }
            confirm_path="$2"
            shift 2
            ;;
        --confirm-corrupt)
            confirm_corrupt=1
            shift
            ;;
        *)
            echo "unknown argument" >&2
            exit 2
            ;;
    esac
done

repo_root="$(realpath "$(git rev-parse --show-toplevel)")"
test "$(pwd -P)" = "$repo_root"
test "$confirm_path" = ".runtime/pdm-fixtures"
test ! -L .runtime
test ! -L .runtime/pdm-fixtures
test ! -L .runtime/recycle
test -d .runtime/pdm-fixtures

fixture_root="$(realpath .runtime/pdm-fixtures)"
test "$fixture_root" = "$repo_root/.runtime/pdm-fixtures"
case "$fixture_root" in
    "$repo_root/.runtime/"*) ;;
    *) echo "fixture target escapes .runtime" >&2; exit 1 ;;
esac

active_project_containers="$(docker ps \
    --filter 'label=com.docker.compose.project=predictive-maintenance-shadow' \
    --format '{{.ID}}')"
if [[ -n "$active_project_containers" ]]; then
    echo "isolated Compose project is still running" >&2
    exit 1
fi
all_container_ids="$(docker ps -q)"
active_bind_sources=""
while IFS= read -r container_id; do
    [[ -n "$container_id" ]] || continue
    active_bind_sources+="$(docker inspect -f '{{range .Mounts}}{{if eq .Type "bind"}}{{println .Source}}{{end}}{{end}}' "$container_id")"
    active_bind_sources+=$'\n'
done <<< "$all_container_ids"
if printf '%s\n' "$active_bind_sources" | grep -Fx -- "$fixture_root" >/dev/null; then
    echo "an active container still mounts the fixture source" >&2
    exit 1
fi

validator_path="$repo_root/deploy/compose/scripts/validate-pdm-fixtures.py"
if [[ ! -f "$validator_path" || -L "$validator_path" || ! -r "$validator_path" ]]; then
    echo "fixture recovery helper is unavailable" >&2
    exit 1
fi
if ! recovery_destination="$(
    IFACTORY_PDM_FIXTURE_RECOVERY_OUTPUT_FD=3 \
    VALEO_PDM_ISOLATED_FIXTURE_MODE=1 \
        uv run --project components/pdm-algorithm --frozen \
        python "$validator_path" "$repo_root" "$confirm_corrupt" \
        3>&2 2>/dev/null
)"; then
    echo "fixture is corrupt or unreadable; repeat with --confirm-corrupt" >&2
    exit 1
fi

case "$recovery_destination" in
    .runtime/recycle/pdm-fixtures-*) ;;
    *) echo "fixture recovery helper returned an invalid result" >&2; exit 1 ;;
esac
printf 'fixture moved to %s\n' "$recovery_destination" >&2
