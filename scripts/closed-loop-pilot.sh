#!/bin/bash
set -euo pipefail

safe_path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
if [[ "${1:-}" != "dashboard" ]]; then
  PATH="$safe_path"
  export PATH
fi
script_dir="${BASH_SOURCE[0]%/*}"
[[ "$script_dir" != "${BASH_SOURCE[0]}" ]] || script_dir="."
repo_root="$(cd "$script_dir/.." && pwd -P)"
compose_file="$repo_root/deploy/compose/closed-loop-pilot.yml"
project_name="ifactory-closed-loop-pilot"
pilot_env_file="$repo_root/.runtime/closed-loop/closed-loop-pilot.env"
generated_env_file="$repo_root/.runtime/closed-loop/state/closed-loop-generated.env"
docker_mode="direct"
docker_command=(docker --host unix:///var/run/docker.sock)

if [[ "${1:-}" != "dashboard" ]]; then
  operation_fd="${IFACTORY_PILOT_OPERATION_LOCK_FD:-}"
  if [[ ! "$operation_fd" =~ ^[0-9]+$ ]] || ! env -i "PATH=$safe_path" \
    python3 "$repo_root/deploy/compose/scripts/pilot_operation_lock.py" verify \
      --control "$repo_root/.runtime/closed-loop" --fd "$operation_fd" \
      >/dev/null 2>&1; then
    exec env -i "PATH=$safe_path" python3 \
      "$repo_root/deploy/compose/scripts/pilot_operation_lock.py" execute \
      --control "$repo_root/.runtime/closed-loop" -- \
      "$repo_root/scripts/closed-loop-pilot.sh" "$@"
  fi
fi

preflight() {
  env -i "PATH=$safe_path" python3 "$repo_root/deploy/compose/scripts/pilot_preflight.py" \
    --repo-root "$repo_root" --env-file "$pilot_env_file" \
    --stage "${1:-base}"
}

select_docker() {
  if env -i "PATH=$safe_path" docker --host unix:///var/run/docker.sock \
    info >/dev/null 2>&1; then
    return
  fi
  if ! command -v sudo >/dev/null 2>&1; then
    printf '%s\n' 'DOCKER_DAEMON_UNAVAILABLE' >&2
    exit 69
  fi
  if ! env -i "PATH=$safe_path" sudo docker --host unix:///var/run/docker.sock \
    info >/dev/null; then
    printf '%s\n' 'DOCKER_DAEMON_UNAVAILABLE' >&2
    exit 69
  fi
  docker_mode="sudo"
  docker_command=(sudo docker --host unix:///var/run/docker.sock)
}

compose() {
  env -i "PATH=$safe_path" "${docker_command[@]}" compose \
    --project-name "$project_name" \
    --env-file "$pilot_env_file" \
    --env-file "$generated_env_file" \
    -f "$compose_file" "$@"
}

usage() {
  printf '%s\n' \
    'usage: scripts/closed-loop-pilot.sh <command> [arguments]' \
    'commands: config, bootstrap-plan, bootstrap-apply HASH, provision-plan ACTOR,' \
    '          provision-apply HASH ACTOR, provision-select-equipment HASH, provision-verify HASH,' \
    '          work-order-status-plan EXTERNAL_REF TARGET,' \
    '          work-order-status-apply EXTERNAL_REF TARGET HASH,' \
    '          acceptance-verify [STAGE], dashboard ARGS...,' \
    '          up, down, summary, reset-plan, reset-apply HASH'
}

case "${1:-}" in
  config)
    preflight
    docker_command=(docker --host unix:///var/run/docker.sock)
    compose --profile continuous --profile bootstrap --profile acceptance config --quiet
    ;;
  bootstrap-plan)
    preflight bootstrap
    select_docker
    compose --profile bootstrap run --rm bootstrap-runner \
      python /pilot/pilot_bootstrap.py plan
    ;;
  bootstrap-apply)
    test -n "${2:-}" || { usage >&2; exit 64; }
    preflight bootstrap
    select_docker
    compose --profile bootstrap run --rm bootstrap-runner \
      python /pilot/pilot_bootstrap.py apply --confirmed-hash "$2"
    ;;
  provision-plan)
    test -n "${2:-}" || { usage >&2; exit 64; }
    preflight provision
    select_docker
    compose up -d integration-db
    compose run --rm integration-migrate
    compose --profile acceptance run --rm --no-deps provision-runner \
      platform-integration provision-plan \
      --tenant-alias ifactory-pilot --actor "$2"
    ;;
  provision-apply)
    test -n "${2:-}" && test -n "${3:-}" || { usage >&2; exit 64; }
    preflight provision
    select_docker
    compose --profile acceptance run --rm --no-deps provision-runner \
      platform-integration provision-apply \
      --tenant-alias ifactory-pilot --plan-hash "$2" \
      --confirmed-hash "$2" --actor "$3"
    compose --profile acceptance run --rm --no-deps provision-runner \
      platform-integration provision-verify \
      --tenant-alias ifactory-pilot --plan-hash "$2"
    compose --profile acceptance run --rm --no-deps provision-runner \
      python /pilot/pilot_select_equipment.py --plan-hash "$2"
    ;;
  provision-select-equipment)
    test -n "${2:-}" || { usage >&2; exit 64; }
    preflight provision
    select_docker
    compose --profile acceptance run --rm --no-deps provision-runner \
      platform-integration provision-verify \
      --tenant-alias ifactory-pilot --plan-hash "$2"
    compose --profile acceptance run --rm --no-deps provision-runner \
      python /pilot/pilot_select_equipment.py --plan-hash "$2"
    ;;
  provision-verify)
    test -n "${2:-}" || { usage >&2; exit 64; }
    preflight provision
    select_docker
    compose --profile acceptance run --rm --no-deps provision-runner \
      platform-integration provision-verify \
      --tenant-alias ifactory-pilot --plan-hash "$2"
    ;;
  work-order-status-plan)
    test -n "${2:-}" && test -n "${3:-}" || { usage >&2; exit 64; }
    preflight continuous
    select_docker
    compose --profile acceptance run --rm --no-deps status-runner \
      python /pilot/pilot_cmms_status.py plan \
      --external-ref "$2" --target "$3"
    ;;
  work-order-status-apply)
    test -n "${2:-}" && test -n "${3:-}" && test -n "${4:-}" || {
      usage >&2
      exit 64
    }
    preflight continuous
    select_docker
    compose --profile acceptance run --rm --no-deps status-runner \
      python /pilot/pilot_cmms_status.py apply \
      --external-ref "$2" --target "$3" --confirmed-hash "$4"
    ;;
  acceptance-verify)
    expected_stage="${2:-CONSISTENT}"
    case "$expected_stage" in
      CONSISTENT|ACTIVE|IN_PROGRESS|COMPLETE|CLEARED) ;;
      *) usage >&2; exit 64 ;;
    esac
    preflight continuous
    select_docker
    compose --profile acceptance run --rm --no-deps provision-runner \
      platform-integration closed-loop-acceptance-verify \
      --tenant-alias ifactory-pilot --expected-stage "$expected_stage" \
      --format json
    ;;
  dashboard)
    shift
    test "$#" -gt 0 || { usage >&2; exit 64; }
    (
      cd "$repo_root/components/thingsboard/dev/automotive-factory"
      ./dev.sh "$@"
    )
    ;;
  up)
    preflight continuous
    select_docker
    compose --profile continuous up -d --build --wait --wait-timeout 300
    ;;
  down)
    preflight
    select_docker
    compose --profile continuous --profile bootstrap --profile acceptance \
      down --remove-orphans
    ;;
  summary)
    preflight continuous
    select_docker
    compose run --rm --no-deps integration-api \
      platform-integration closed-loop-summary \
      --tenant-alias ifactory-pilot --format json
    ;;
  reset-plan)
    select_docker
    env -i "PATH=$safe_path" python3 \
      "$repo_root/deploy/compose/scripts/pilot_reset.py" plan \
      --docker-mode "$docker_mode" --env-file "$pilot_env_file"
    ;;
  reset-apply)
    test -n "${2:-}" || { usage >&2; exit 64; }
    select_docker
    env -i "PATH=$safe_path" python3 \
      "$repo_root/deploy/compose/scripts/pilot_reset.py" apply \
      --confirmed-hash "$2" --docker-mode "$docker_mode" \
      --env-file "$pilot_env_file"
    ;;
  *)
    usage >&2
    exit 64
    ;;
esac
