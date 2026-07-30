#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

repo_root="$(git rev-parse --show-toplevel)"
repo_root="$(realpath "$repo_root")"
test "$(pwd -P)" = "$repo_root"
rg -q '^\.runtime/$' .gitignore
test ! -L .runtime
install -d -m 0700 .runtime
test "$(realpath .runtime)" = "$repo_root/.runtime"
test ! -e .runtime/pdm-fixtures
test ! -L .runtime/pdm-fixtures

fixture_staging_dir=""
cleanup() {
    if [[ -n "$fixture_staging_dir" && -d "$fixture_staging_dir" ]]; then
        find "$fixture_staging_dir" -depth -delete
    fi
}
trap cleanup EXIT

fixture_staging_dir="$(mktemp -d .runtime/pdm-fixtures.XXXXXX)"
uv run --project components/pdm-algorithm --frozen \
    valeo-pdm prepare-isolated-fixtures \
    --manifest components/pdm-algorithm/configs/isolated_fixture_manifest.yaml \
    --output "$fixture_staging_dir"
test -f "$fixture_staging_dir/manifest.runtime.yaml"
test -d "$fixture_staging_dir/objects"
find "$fixture_staging_dir" -type f -exec chmod 0444 {} +
find "$fixture_staging_dir" -type d -exec chmod 0555 {} +
mv -- "$fixture_staging_dir" .runtime/pdm-fixtures
fixture_staging_dir=""
