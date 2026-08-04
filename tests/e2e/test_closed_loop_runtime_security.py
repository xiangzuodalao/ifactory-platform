from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil

import pytest


ROOT = Path(__file__).parents[2]
PREFLIGHT_SOURCE = ROOT / "deploy/compose/scripts/pilot_preflight.py"


def _load_preflight(tmp_path: Path):
    target = tmp_path / "repo/deploy/compose/scripts/pilot_preflight.py"
    target.parent.mkdir(parents=True)
    shutil.copy2(PREFLIGHT_SOURCE, target)
    spec = importlib.util.spec_from_file_location("isolated_pilot_preflight", target)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, target.parents[3]


def _runtime(tmp_path: Path):
    preflight, repo = _load_preflight(tmp_path)
    runtime = repo / ".runtime"
    runtime.mkdir(mode=0o700)
    control = runtime / "closed-loop"
    control.mkdir(mode=0o700)
    state_lock = control / preflight.STATE_LOCK_NAME
    state_lock.touch(mode=0o600)
    state_lock.chmod(0o600)
    state = control / "state"
    state.mkdir(mode=0o700)
    (state / "receipts").mkdir(mode=0o700)
    secrets = runtime / "secrets"
    secrets.mkdir(mode=0o700)
    values = {
        "PILOT_RUNTIME_DIR": str(runtime),
        "PILOT_STATE_DIR": str(state),
        "PILOT_STATE_LOCK_FILE": str(state_lock),
        "PILOT_HOST_UID": str(os.getuid()),
        "PILOT_HOST_GID": str(os.getgid()),
    }
    for key, filename in preflight.SECRET_FILES.items():
        path = secrets / filename
        path.write_text("fixture-secret\n", encoding="utf-8")
        path.chmod(0o600)
        values[key] = str(path)
    env_file = control / preflight.ENV_NAME
    generated_env = state / preflight.GENERATED_ENV_NAME

    def write_env() -> None:
        base = {
            key: value for key, value in values.items() if key not in preflight.GENERATED_ENV_KEYS
        }
        env_file.write_text(
            "".join(f"{key}={value}\n" for key, value in base.items()), encoding="utf-8"
        )
        env_file.chmod(0o600)
        generated_env.write_text(
            "".join(f"{key}={values.get(key, '')}\n" for key in preflight.GENERATED_ENV_KEYS),
            encoding="utf-8",
        )
        generated_env.chmod(0o600)

    write_env()
    return preflight, repo, runtime, state, env_file, values, write_env


def test_preflight_resolves_the_actual_repository_root(tmp_path: Path) -> None:
    preflight, repo, _runtime_dir, _state, env_file, _values, _write = _runtime(tmp_path)
    assert preflight.validate(repo, env_file) is not None


def test_preflight_rejects_runtime_or_env_binding_drift(tmp_path: Path) -> None:
    preflight, repo, runtime, state, env_file, values, write_env = _runtime(tmp_path)
    values["PILOT_RUNTIME_DIR"] = str(runtime.parent / "elsewhere")
    write_env()
    with pytest.raises(preflight.PreflightError, match="PILOT_RUNTIME_BINDING_INVALID"):
        preflight.validate(repo, env_file)
    values["PILOT_RUNTIME_DIR"] = str(runtime)
    values["PILOT_STATE_DIR"] = str(state)
    write_env()
    env_file.chmod(0o644)
    with pytest.raises(preflight.PreflightError, match="PILOT_ENV_FILE_INVALID"):
        preflight.validate(repo, env_file)


def test_preflight_enforces_disjoint_exact_generated_keys(tmp_path: Path) -> None:
    preflight, repo, _runtime_dir, state, env_file, _values, _write = _runtime(tmp_path)
    generated = state / preflight.GENERATED_ENV_NAME
    generated.write_text(
        generated.read_text(encoding="utf-8") + "UNEXPECTED=value\n",
        encoding="utf-8",
    )
    generated.chmod(0o600)
    with pytest.raises(
        preflight.PreflightError,
        match="PILOT_GENERATED_ENV_FILE_INVALID",
    ):
        preflight.validate(repo, env_file)

    generated.write_text(
        "".join(f"{key}=\n" for key in preflight.GENERATED_ENV_KEYS),
        encoding="utf-8",
    )
    generated.chmod(0o600)
    with env_file.open("a", encoding="utf-8") as stream:
        stream.write("PILOT_CMMS_CREDENTIAL=duplicate-location\n")
    with pytest.raises(preflight.PreflightError, match="PILOT_ENV_FILE_INVALID"):
        preflight.validate(repo, env_file)


def test_continuous_stage_binds_receipts_credentials_and_fixtures(tmp_path: Path) -> None:
    preflight, repo, runtime, state, env_file, values, write_env = _runtime(tmp_path)
    tenant = "00000000-0000-4000-8000-000000000001"
    tb_tenant = "00000000-0000-4000-8000-000000000002"
    approver = "00000000-0000-4000-8000-000000000003"
    equipment = "00000000-0000-4000-8000-000000000101"
    values.update(
        {
            "PLATFORM_INTEGRATION_TENANT_ID": tenant,
            "PLATFORM_INTEGRATION_TB_TENANT_ID": tb_tenant,
            "PLATFORM_INTEGRATION_CMMS_COMPANY_ID": "42",
            "PLATFORM_INTEGRATION_APPROVER_TB_USER_ID": approver,
            "PLATFORM_INTEGRATION_PILOT_WORK_ORDER_EQUIPMENT_ID": equipment,
            "PILOT_TB_CREDENTIAL": '{"kind":"thingsboard_bearer","value":"tb"}',
            "PILOT_CMMS_CREDENTIAL": '{"kind":"cmms_bearer","value":"cmms"}',
            "PILOT_PDM_CREDENTIAL": '{"kind":"opaque_bearer","value":"pdm"}',
            "VALEO_PDM_PREDICTION_V2_BEARER_TOKEN": "pdm",
        }
    )
    write_env()
    receipts = state / "receipts"
    bootstrap = {
        "schema_version": "closed-loop-bootstrap-v1",
        "status": "SUCCEEDED",
        "plan_sha256": "a" * 64,
        "tenant_id": tenant,
        "tb_tenant_id": tb_tenant,
        "tb_approver_user_id": approver,
        "cmms_company_id": 42,
    }
    provision = {
        "schema_version": "closed-loop-provision-v1",
        "status": "SUCCEEDED",
        "plan_sha256": "b" * 64,
        "tenant_id": tenant,
        "tb_tenant_id": tb_tenant,
        "cmms_company_id": 42,
        "equipment_id": equipment,
    }
    for name, payload in (
        ("closed-loop-bootstrap.json", bootstrap),
        ("closed-loop-provision.json", provision),
    ):
        path = receipts / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        path.chmod(0o600)
    fixtures = runtime / "pdm-fixtures"
    objects = fixtures / "objects"
    objects.mkdir(parents=True)
    manifest = fixtures / "manifest.runtime.yaml"
    manifest.write_text("schema_version: 1\n", encoding="utf-8")
    manifest.chmod(0o444)
    objects.chmod(0o555)
    fixtures.chmod(0o555)
    assert preflight.validate(repo, env_file, "continuous") is not None
    values["VALEO_PDM_PREDICTION_V2_BEARER_TOKEN"] = "drifted"
    write_env()
    with pytest.raises(preflight.PreflightError, match="PDM_CREDENTIAL_BINDING_INVALID"):
        preflight.validate(repo, env_file, "continuous")


def test_reset_archives_both_receipts_and_clears_generated_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = importlib.util.spec_from_file_location(
        "isolated_pilot_reset", ROOT / "deploy/compose/scripts/pilot_reset.py"
    )
    assert spec is not None and spec.loader is not None
    reset = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reset)
    runtime = tmp_path / "runtime"
    receipts = runtime / "receipts"
    receipts.mkdir(parents=True, mode=0o700)
    runtime.chmod(0o700)
    synced_directories: list[Path] = []
    original_fsync_directory = reset.fsync_directory

    def record_fsync_directory(path: Path) -> None:
        synced_directories.append(path)
        original_fsync_directory(path)

    monkeypatch.setattr(reset, "fsync_directory", record_fsync_directory)
    for source_name, archive_name in (
        ("closed-loop-bootstrap.json", "closed-loop-bootstrap"),
        ("closed-loop-provision.json", "closed-loop-provision"),
    ):
        source = receipts / source_name
        raw = (json.dumps({"status": "SUCCEEDED", "name": source_name}) + "\n").encode()
        source.write_bytes(raw)
        source.chmod(0o600)
        binding = reset._receipt_binding(source)
        staged = reset._stage_receipt(
            runtime,
            binding,
            source_name,
            archive_name,
        )
        assert staged is not None
        reset._commit_staged_receipts([staged])
        assert not source.exists()
        assert len(list((runtime / "recycle").glob(f"{archive_name}-*.json"))) == 1

    assert synced_directories[:3] == [runtime, runtime / "recycle", receipts]

    env_file = runtime / "closed-loop-generated.env"
    env_file.write_text(
        "corrupt generated state that reset must recover\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    reset._clear_generated_env(env_file)
    content = env_file.read_text(encoding="utf-8")
    assert all(f"{key}=\n" in content for key in reset.GENERATED_ENV_KEYS)
    assert set(line.split("=", 1)[0] for line in content.splitlines()) == set(
        reset.GENERATED_ENV_KEYS
    )
