from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "deploy/compose/scripts/pilot_cmms_status.py"
SPEC = importlib.util.spec_from_file_location("pilot_cmms_status", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
status_control = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(status_control)

TENANT_ID = "00000000-0000-4000-8000-000000000001"
EXTERNAL_REF = "10000000-0000-4000-8000-000000000001"
CORRELATION_ID = "20000000-0000-4000-8000-000000000001"
EQUIPMENT_ID = "30000000-0000-4000-8000-000000000001"
TB_ALARM_ID = "40000000-0000-4000-8000-000000000001"
BEARER = "header.payload.signature"


class FakeCmms:
    def __init__(self) -> None:
        self.current_status = "OPEN"
        self.event_version = 0
        self.policy_version = "pilot-v1"
        self.patch_calls = 0
        self.lose_patch_response = False

    def snapshot(self) -> dict[str, object]:
        return {
            "id": 73,
            "title": "content intentionally excluded from the plan",
            "description": "content intentionally excluded from the plan",
            "status": self.current_status,
            "event_version": self.event_version,
            "asset": {"id": 41, "name": "LINE-A-CNC-01"},
            "external_source": "PDM_FORECAST",
            "external_ref": EXTERNAL_REF,
            "correlation_id": CORRELATION_ID,
            "equipment_id": EQUIPMENT_ID,
            "tb_alarm_id": TB_ALARM_ID,
            "model_profile_id": "cnc-vibration-rms",
            "model_info_id": "model-info-2026-08-04",
            "policy_version": self.policy_version,
        }

    def request(
        self,
        method: str,
        path: str,
        *,
        bearer: str,
        body: object | None = None,
    ) -> tuple[int, object]:
        assert bearer == BEARER
        if method == "GET" and path == "/api/auth/me":
            return 200, {"companyId": 42, "email": "pilot@example.test"}
        if method == "GET" and path.startswith("/api/work-orders/by-external-ref?"):
            assert "source=PDM_FORECAST" in path
            assert f"ref={EXTERNAL_REF}" in path
            return 200, self.snapshot()
        if method == "PATCH" and path == "/api/work-orders/73/change-status":
            self.patch_calls += 1
            assert body in ({"status": "IN_PROGRESS"}, {"status": "COMPLETE"})
            if self.lose_patch_response:
                raise status_control.StatusControlError("CMMS_PROVIDER_RESPONSE_LOST")
            self.current_status = str(body["status"])
            self.event_version += 1
            return 200, self.snapshot()
        raise AssertionError(f"unexpected request: {method} {path}")


def _environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    (tmp_path / "receipts").mkdir(mode=0o700, exist_ok=True)
    state_lock = tmp_path / ".state.lock"
    state_lock.touch(mode=0o600)
    state_lock.chmod(0o600)
    monkeypatch.setattr(status_control, "STATE_LOCK_PATH", str(state_lock))
    values = {
        "PLATFORM_INTEGRATION_TENANT_ID": TENANT_ID,
        "PLATFORM_INTEGRATION_CMMS_COMPANY_ID": "42",
        "PLATFORM_INTEGRATION_CMMS_BASE_URL": "http://cmms-gateway:8080",
        "PILOT_CMMS_CREDENTIAL": json.dumps(
            {"kind": "cmms_bearer", "value": BEARER}, separators=(",", ":")
        ),
        "PILOT_RUNTIME_ROOT": str(tmp_path),
        "PILOT_STATE_LOCK_FILE": str(state_lock),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def _install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, provider: FakeCmms) -> None:
    _environment(monkeypatch, tmp_path)
    monkeypatch.setattr(status_control, "_request", provider.request)


def test_plan_is_read_only_deterministic_and_freezes_full_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider = FakeCmms()
    _install(monkeypatch, tmp_path, provider)

    first = status_control.build_plan(EXTERNAL_REF, "IN_PROGRESS")
    second = status_control.build_plan(EXTERNAL_REF, "IN_PROGRESS")

    assert first == second
    assert provider.patch_calls == 0
    assert first == {
        "schema_version": "closed-loop-cmms-status-v1",
        "operation": "advance_predictive_work_order_status",
        "tenant_id": TENANT_ID,
        "cmms_company_id": 42,
        "work_order_id": 73,
        "asset_id": 41,
        "external_source": "PDM_FORECAST",
        "external_ref": EXTERNAL_REF,
        "correlation_id": CORRELATION_ID,
        "equipment_id": EQUIPMENT_ID,
        "tb_alarm_id": TB_ALARM_ID,
        "model_profile_id": "cnc-vibration-rms",
        "model_info_id": "model-info-2026-08-04",
        "policy_version": "pilot-v1",
        "current_status": "OPEN",
        "integration_event_version": 0,
        "target_status": "IN_PROGRESS",
        "write_required": True,
        "plan_sha256": first["plan_sha256"],
    }
    serialized = json.dumps(first)
    assert len(first["plan_sha256"]) == 64
    assert BEARER not in serialized
    assert "content intentionally excluded" not in serialized


def test_malformed_confirmation_stops_before_any_provider_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider = FakeCmms()
    _install(monkeypatch, tmp_path, provider)
    monkeypatch.setattr(
        status_control,
        "_request",
        lambda *_args, **_kwargs: pytest.fail("provider access must not occur"),
    )

    with pytest.raises(
        status_control.StatusControlError,
        match="CMMS_STATUS_CONFIRMATION_MISMATCH",
    ):
        status_control.apply(EXTERNAL_REF, "IN_PROGRESS", "not-a-sha256")


@pytest.mark.parametrize(
    ("drift", "value"),
    (("event_version", 1), ("policy_version", "pilot-v2")),
)
def test_apply_rejects_version_or_identity_drift_without_writing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    drift: str,
    value: object,
) -> None:
    provider = FakeCmms()
    _install(monkeypatch, tmp_path, provider)
    plan = status_control.build_plan(EXTERNAL_REF, "COMPLETE")
    setattr(provider, drift, value)

    with pytest.raises(
        status_control.StatusControlError,
        match="CMMS_STATUS_CONFIRMATION_MISMATCH",
    ):
        status_control.apply(EXTERNAL_REF, "COMPLETE", str(plan["plan_sha256"]))

    assert provider.patch_calls == 0
    assert not list((tmp_path / "receipts").glob("cmms-status-*.json"))


def test_confirmed_apply_performs_exactly_one_patch_and_audits_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider = FakeCmms()
    _install(monkeypatch, tmp_path, provider)
    plan = status_control.build_plan(EXTERNAL_REF, "IN_PROGRESS")

    result = status_control.apply(EXTERNAL_REF, "IN_PROGRESS", str(plan["plan_sha256"]))

    assert provider.patch_calls == 1
    assert result["result"] == "STATUS_ADVANCED"
    assert result["previous_status"] == "OPEN"
    assert result["current_status"] == "IN_PROGRESS"
    assert result["integration_event_version"] == 1
    receipt = tmp_path / "receipts" / str(result["receipt"])
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert json.loads(receipt.read_text(encoding="utf-8")) == result
    assert BEARER not in receipt.read_text(encoding="utf-8")


def test_lost_patch_response_is_not_retried_and_same_plan_is_permanently_fenced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider = FakeCmms()
    provider.lose_patch_response = True
    _install(monkeypatch, tmp_path, provider)
    plan = status_control.build_plan(EXTERNAL_REF, "IN_PROGRESS")

    with pytest.raises(
        status_control.StatusControlError,
        match="CMMS_STATUS_WRITE_OUTCOME_UNKNOWN",
    ):
        status_control.apply(EXTERNAL_REF, "IN_PROGRESS", str(plan["plan_sha256"]))
    assert provider.patch_calls == 1

    receipt = tmp_path / "receipts" / f"cmms-status-{plan['plan_sha256']}.json"
    recorded = json.loads(receipt.read_text(encoding="utf-8"))
    assert recorded["status"] == "OUTCOME_UNKNOWN"
    assert recorded["code"] == "CMMS_STATUS_WRITE_OUTCOME_UNKNOWN"
    assert BEARER not in receipt.read_text(encoding="utf-8")

    with pytest.raises(
        status_control.StatusControlError,
        match="CMMS_STATUS_PLAN_ALREADY_APPLIED_OR_UNCERTAIN",
    ):
        status_control.apply(EXTERNAL_REF, "IN_PROGRESS", str(plan["plan_sha256"]))
    assert provider.patch_calls == 1


def test_already_at_target_is_confirmable_without_a_patch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider = FakeCmms()
    provider.current_status = "IN_PROGRESS"
    provider.event_version = 1
    _install(monkeypatch, tmp_path, provider)
    plan = status_control.build_plan(EXTERNAL_REF, "IN_PROGRESS")

    assert plan["write_required"] is False
    result = status_control.apply(EXTERNAL_REF, "IN_PROGRESS", str(plan["plan_sha256"]))
    assert result["result"] == "ALREADY_AT_TARGET"
    assert provider.patch_calls == 0


def test_credential_envelope_and_base_url_are_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    provider = FakeCmms()
    _install(monkeypatch, tmp_path, provider)
    monkeypatch.setenv(
        "PILOT_CMMS_CREDENTIAL",
        '{"kind":"cmms_bearer","value":"one","value":"two"}',
    )
    with pytest.raises(
        status_control.StatusControlError,
        match="PILOT_CMMS_CREDENTIAL_INVALID",
    ):
        status_control.build_plan(EXTERNAL_REF, "IN_PROGRESS")

    monkeypatch.setenv(
        "PILOT_CMMS_CREDENTIAL",
        json.dumps({"kind": "cmms_bearer", "value": BEARER}),
    )
    monkeypatch.setenv("PLATFORM_INTEGRATION_CMMS_BASE_URL", "http://cmms-api:8080")
    with pytest.raises(
        status_control.StatusControlError,
        match="PLATFORM_INTEGRATION_CMMS_BASE_URL_INVALID",
    ):
        status_control.build_plan(EXTERNAL_REF, "IN_PROGRESS")
