from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
from uuid import UUID, uuid5

import pytest


ROOT = Path(__file__).parents[2]


def _module(relative: str, name: str):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap = _module("deploy/compose/scripts/pilot_bootstrap.py", "pilot_bootstrap")
selector = _module("deploy/compose/scripts/pilot_select_equipment.py", "pilot_select_equipment")
reset = _module("deploy/compose/scripts/pilot_reset.py", "pilot_reset")


def _bootstrap_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "CMMS_PILOT_ADMIN_EMAIL": "pilot@example.test",
        "PLATFORM_INTEGRATION_TENANT_ID": "00000000-0000-4000-8000-000000000001",
        "PLATFORM_INTEGRATION_TB_BASE_URL": "http://tb-relay:8080",
        "CMMS_BOOTSTRAP_BASE_URL": "http://cmms-api:8080",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_bootstrap_plan_is_deterministic_and_secret_free(monkeypatch: pytest.MonkeyPatch) -> None:
    _bootstrap_environment(monkeypatch)
    monkeypatch.setenv(
        "PILOT_TB_CREDENTIAL", '{"kind":"thingsboard_bearer","value":"never-in-plan"}'
    )
    monkeypatch.setattr(
        bootstrap,
        "_tb_identity",
        lambda _plan: (
            "00000000-0000-4000-8000-000000000002",
            "00000000-0000-4000-8000-000000000003",
        ),
    )
    first = bootstrap.build_plan()
    second = bootstrap.build_plan()
    assert first == second
    assert len(first["plan_sha256"]) == 64
    serialized = json.dumps(first)
    assert "never-in-plan" not in serialized
    assert "password" not in serialized.lower()
    assert first["company_name"] == "iFactory Closed Loop Pilot"
    assert first["tb_tenant_id"] == "00000000-0000-4000-8000-000000000002"


def test_bootstrap_confirmation_mismatch_stops_before_external_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bootstrap_environment(monkeypatch)
    monkeypatch.setenv("PILOT_TB_CREDENTIAL", '{"kind":"thingsboard_bearer","value":"test-token"}')
    monkeypatch.setattr(
        bootstrap,
        "_tb_identity",
        lambda _plan: (
            "00000000-0000-4000-8000-000000000002",
            "00000000-0000-4000-8000-000000000003",
        ),
    )
    plan = bootstrap.build_plan()
    monkeypatch.setattr(
        bootstrap, "_tb_identity", lambda _plan: pytest.fail("external access must not occur")
    )
    with pytest.raises(bootstrap.BootstrapError, match="BOOTSTRAP_CONFIRMATION_MISMATCH"):
        bootstrap.apply(plan, "0" * 64)


def test_bootstrap_apply_rejects_thingsboard_identity_drift_before_cmms_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _bootstrap_environment(monkeypatch)
    monkeypatch.setenv("PILOT_TB_CREDENTIAL", '{"kind":"thingsboard_bearer","value":"test-token"}')
    identities = iter(
        [
            (
                "00000000-0000-4000-8000-000000000002",
                "00000000-0000-4000-8000-000000000003",
            ),
            (
                "00000000-0000-4000-8000-000000000004",
                "00000000-0000-4000-8000-000000000005",
            ),
        ]
    )
    monkeypatch.setattr(bootstrap, "_tb_identity", lambda _plan: next(identities))
    plan = bootstrap.build_plan()
    tmp_path.chmod(0o700)
    (tmp_path / "receipts").mkdir(mode=0o700)
    state_lock = tmp_path / ".state.lock"
    state_lock.touch(mode=0o600)
    state_lock.chmod(0o600)
    monkeypatch.setattr(bootstrap, "STATE_LOCK_PATH", str(state_lock))
    monkeypatch.setenv("PILOT_STATE_LOCK_FILE", str(state_lock))
    monkeypatch.setenv("PILOT_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setattr(
        bootstrap, "_cmms_identity", lambda _plan: pytest.fail("CMMS write must not occur")
    )
    with pytest.raises(bootstrap.BootstrapError, match="THINGSBOARD_IDENTITY_DRIFTED"):
        bootstrap.apply(plan, plan["plan_sha256"])


def test_bootstrap_updates_only_expected_secure_env_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / "closed-loop-pilot.env"
    env_file.write_text(
        "# retained\nUNRELATED=value\nPLATFORM_INTEGRATION_CMMS_COMPANY_ID=\n",
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    monkeypatch.setenv("PILOT_ENV_FILE", str(env_file))
    bootstrap._update_env(
        {
            "PLATFORM_INTEGRATION_CMMS_COMPANY_ID": "42",
            "PLATFORM_INTEGRATION_TB_TENANT_ID": "00000000-0000-4000-8000-000000000002",
        }
    )
    content = env_file.read_text(encoding="utf-8")
    assert "# retained" in content
    assert "UNRELATED=value" in content
    assert "PLATFORM_INTEGRATION_CMMS_COMPANY_ID=42" in content
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_bootstrap_rejects_non_0600_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    env_file = tmp_path / "closed-loop-pilot.env"
    env_file.write_text("A=B\n", encoding="utf-8")
    env_file.chmod(0o644)
    monkeypatch.setenv("PILOT_ENV_FILE", str(env_file))
    with pytest.raises(bootstrap.BootstrapError, match="PILOT_ENV_FILE_INVALID"):
        bootstrap._update_env({"A": "C"})


def test_equipment_selector_ignores_unrelated_thingsboard_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = UUID("00000000-0000-4000-8000-000000000001")
    tb_tenant_id = "00000000-0000-4000-8000-000000000002"
    tb_device_id = "00000000-0000-4000-8000-000000000003"
    equipment_id = str(uuid5(tenant_id, f"pilot-equipment:{tb_device_id}"))
    for key, value in {
        "PLATFORM_INTEGRATION_TENANT_ID": str(tenant_id),
        "PLATFORM_INTEGRATION_TB_TENANT_ID": tb_tenant_id,
        "PLATFORM_INTEGRATION_CMMS_COMPANY_ID": "2",
        "PLATFORM_INTEGRATION_TB_BASE_URL": "http://tb-relay:8080",
        "PLATFORM_INTEGRATION_CMMS_BASE_URL": "http://cmms-gateway:8080",
        "PILOT_TB_CREDENTIAL": '{"kind":"thingsboard_bearer","value":"tb-token"}',
        "PILOT_CMMS_CREDENTIAL": '{"kind":"cmms_bearer","value":"cmms-token"}',
    }.items():
        monkeypatch.setenv(key, value)
    devices = [
        {
            "id": {"id": tb_device_id},
            "name": selector.DEVICE_NAME,
            "type": "CNC",
        },
        *[
            {
                "id": {"id": f"00000000-0000-4000-8000-{index:012d}"},
                "name": f"UNRELATED-{index}",
                "type": "default",
            }
            for index in range(10, 20)
        ],
    ]
    responses = iter(
        [
            (200, {"tenantId": {"id": tb_tenant_id}}),
            (200, {"hasNext": False, "totalElements": len(devices), "data": devices}),
            (
                200,
                [
                    {"key": "equipment_id", "value": equipment_id},
                    {"key": "cmms_asset_id", "value": 1},
                ],
            ),
            (200, {"companyId": 2}),
            (200, {"id": 1, "equipment_id": equipment_id}),
        ]
    )
    monkeypatch.setattr(selector, "_request", lambda *args, **kwargs: next(responses))
    updates: list[dict[str, str]] = []
    monkeypatch.setattr(selector, "_update_env", updates.append)
    monkeypatch.setattr(selector, "_write_receipt", lambda plan_hash, result: None)

    result = selector._select_locked("a" * 64)

    assert result["equipment_id"] == equipment_id
    assert updates == [{"PLATFORM_INTEGRATION_PILOT_WORK_ORDER_EQUIPMENT_ID": equipment_id}]


def test_reset_plan_accepts_only_exact_compose_labeled_volumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str]) -> subprocess.CompletedProcess[str]:
        if "info" in command:
            return subprocess.CompletedProcess(command, 0, "26.1.0\n", "")
        if "ls" in command:
            name = command[command.index("--filter") + 1].removeprefix("name=^").removesuffix("$")
            return subprocess.CompletedProcess(command, 0, f"{name}\n", "")
        logical = command[-1].removeprefix(f"{reset.PROJECT}_")
        value = [
            {
                "Name": command[-1],
                "Driver": "local",
                "Labels": {
                    "com.docker.compose.project": reset.PROJECT,
                    "com.docker.compose.volume": logical,
                },
            }
        ]
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "")

    monkeypatch.setattr(reset, "_run", fake_run)
    monkeypatch.setattr(
        reset,
        "_runtime_binding",
        lambda _env: (
            Path("/runtime"),
            {},
            {
                "env_file": ".runtime/closed-loop/closed-loop-pilot.env",
                "generated_values_set": {},
                "receipts": {
                    "bootstrap": {"present": False},
                    "provision": {"present": False},
                },
            },
        ),
    )
    plan = reset._build_plan_unlocked(Path("ignored"), "direct")
    assert [item["volume_label"] for item in plan["volumes"]] == list(reset.LOGICAL_VOLUMES)
    assert all(item["name"].startswith(f"{reset.PROJECT}_") for item in plan["volumes"])
    assert len(plan["plan_sha256"]) == 64


def test_reset_confirmation_mismatch_never_stops_or_removes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reset, "_run", lambda command: pytest.fail(f"unexpected command: {command}")
    )
    plan = {
        "schema_version": "closed-loop-reset-v1",
        "project": reset.PROJECT,
        "operation": "stop_project_and_remove_dedicated_volumes",
        "volumes": [],
        "plan_sha256": "1" * 64,
    }
    with pytest.raises(reset.ResetError, match="RESET_CONFIRMATION_MISMATCH"):
        reset.apply(plan, "2" * 64, Path("unused"))


def test_reset_plan_fails_closed_when_docker_daemon_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        reset,
        "_runtime_binding",
        lambda _env: (Path("/runtime"), {}, {"receipts": {}}),
    )
    monkeypatch.setattr(
        reset,
        "_run",
        lambda command: subprocess.CompletedProcess(command, 1, "", "permission denied"),
    )
    with pytest.raises(reset.ResetError, match="DOCKER_DAEMON_UNAVAILABLE"):
        reset._build_plan_unlocked(Path("ignored"), "direct")
