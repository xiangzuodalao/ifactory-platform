"""Offline contract for CMMS configuration, records and confirmation gates."""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import os
import pickle
import shutil
import stat
from unittest.mock import patch
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from e2e.support import SafeRuntimeFixture
from ifactory_cmms_deploy.config import BootstrapConfig, RuntimeConfig
from ifactory_cmms_deploy.errors import DeploymentError
from ifactory_cmms_deploy.records import (
    ACCEPTANCE_RECEIPT_SCHEMA,
    ACTION_RANK,
    BOOTSTRAP_RECEIPT_SCHEMA,
    BUDGET_LEDGER_SCHEMA,
    BUDGET_RECOVERY_RECEIPT_SCHEMA,
    START_PERMIT_SCHEMA,
    ActionCode,
    ActionRegistry,
    ActionTargetKind,
    ApiKeyCapturePlanBinding,
    ApiKeyCleanupOutcome,
    ApiKeyCleanupPlanBinding,
    ApplicationResultCode,
    BootstrapPlanBindings,
    BootstrapState,
    ClaimedApplyContext,
    ConfirmedDeploymentPlan,
    CredentialIdentity,
    CredentialPlanBinding,
    DeploymentPlan,
    DeploymentSnapshot,
    DeploymentWriteLease,
    FailClosedEvidence,
    FixedRecordSchema,
    GatewayListenerAbsentProof,
    GatewayListenerChallenge,
    GatewayMode,
    InvitationProbePlanBinding,
    LicenseMode,
    Operation,
    PlanApplicationRecord,
    PlanApplicationState,
    PlanAttemptReservation,
    PlannedAction,
    RuntimeProfile,
    SecureFileStatBinding,
    SourceBinding,
    SourceStatus,
    StateRecord,
    acquire_deployment_write_lease,
    canonical_json_bytes,
    claim_plan_application,
    load_confirmed_plan,
    require_exact_record_fields,
    reserve_plan_attempt,
    strict_canonical_json_loads,
    write_plan,
)


SENTINEL_SECRET = "cmms-test-secret://sentinel"
NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
COMPOSE_TARGET = "compose:ifactory-cmms-dev"
RECEIPT_TARGET = "receipt:cmms-bootstrap"
ROLE_TARGET = "ifactory-pdm-runtime"
PHASE2_TARGET = "predictive-maintenance-shadow.env"
PROBE_SLOT = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def safe_runtime(tmp_path: Path) -> SafeRuntimeFixture:
    return SafeRuntimeFixture(tmp_path)


def _private_file(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def _env_bytes(rows: list[tuple[str, str]]) -> bytes:
    return ("\n".join(f"{key}={value}" for key, value in rows) + "\n").encode()


def _write_config_tree(root: Path, *, mode: str = "offline") -> dict[str, Path]:
    runtime = root / ".runtime"
    secrets = runtime / "secrets"
    secrets.mkdir(parents=True, mode=0o700)
    runtime.chmod(0o700)
    secrets.chmod(0o700)

    refs = {
        name: secrets / name
        for name in (
            "postgres-password",
            "minio-root-user",
            "minio-root-password",
            "jwt-secret",
            "license-key",
            "license-file",
            "super-admin-current",
            "super-admin-candidate",
            "organization-admin-current",
            "organization-admin-candidate",
            "runtime-user-current",
            "runtime-user-candidate",
        )
    }
    runtime_rows = [
        ("LICENSE_MODE", mode),
        ("POSTGRES_USER", "atlas_user"),
        ("POSTGRES_DB", "atlas"),
        ("POSTGRES_PASSWORD_FILE", str(refs["postgres-password"])),
        ("MINIO_ROOT_USER_FILE", str(refs["minio-root-user"])),
        ("MINIO_ROOT_PASSWORD_FILE", str(refs["minio-root-password"])),
        ("JWT_SECRET_KEY_FILE", str(refs["jwt-secret"])),
        ("LICENSE_KEY_FILE", str(refs["license-key"])),
        (
            "LICENSE_FILE_PATH",
            str(refs["license-file"]) if mode == "offline" else "",
        ),
        ("ALLOWED_ORGANIZATION_ADMINS", "orgadmin@example.test"),
    ]
    bootstrap_rows = [
        ("ORGANIZATION_ADMIN_EMAIL", "orgadmin@example.test"),
        ("RUNTIME_USER_EMAIL", "runtime@example.test"),
        ("ROLE_EXTERNAL_ID", "ifactory-pdm-runtime"),
        ("API_KEY_LABEL", "ifactory-pdm-runtime"),
        ("SUPER_ADMIN_CURRENT_PASSWORD_FILE", str(refs["super-admin-current"])),
        (
            "SUPER_ADMIN_CANDIDATE_PASSWORD_FILE",
            str(refs["super-admin-candidate"]),
        ),
        (
            "ORGANIZATION_ADMIN_CURRENT_PASSWORD_FILE",
            str(refs["organization-admin-current"]),
        ),
        (
            "ORGANIZATION_ADMIN_CANDIDATE_PASSWORD_FILE",
            str(refs["organization-admin-candidate"]),
        ),
        ("RUNTIME_USER_CURRENT_PASSWORD_FILE", str(refs["runtime-user-current"])),
        (
            "RUNTIME_USER_CANDIDATE_PASSWORD_FILE",
            str(refs["runtime-user-candidate"]),
        ),
    ]
    files = {
        "runtime": _private_file(
            runtime / "cmms-development.env",
            _env_bytes(runtime_rows),
        ),
        "bootstrap": _private_file(
            runtime / "cmms-bootstrap.env",
            _env_bytes(bootstrap_rows),
        ),
        "frontend": _private_file(
            runtime / "cmms-frontend.env",
            b"HOST=127.0.0.1\nPORT=3001\nAPI_URL=/api\n",
        ),
    }
    return files | refs


def _replace_env_value(path: Path, key: str, value: str) -> None:
    rows = path.read_text(encoding="utf-8").splitlines()
    replaced = [f"{key}={value}" if row.startswith(f"{key}=") else row for row in rows]
    path.write_text("\n".join(replaced) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _delete_env_key(path: Path, key: str) -> None:
    rows = [
        row
        for row in path.read_text(encoding="utf-8").splitlines()
        if not row.startswith(f"{key}=")
    ]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    path.chmod(0o600)


def test_runtime_and_bootstrap_config_load_exact_frozen_surfaces(tmp_path: Path) -> None:
    _write_config_tree(tmp_path)

    runtime = RuntimeConfig.load(tmp_path)
    bootstrap = BootstrapConfig.load(tmp_path)

    assert runtime.compose_project == "ifactory-cmms-dev"
    assert runtime.public_browser_origin == "http://cmms.localhost:3000"
    assert runtime.api_bind == "127.0.0.1:8082"
    assert runtime.db_url == "127.0.0.1:5433/atlas"
    assert runtime.license_mode == "offline"
    assert runtime.allowed_organization_admin == "orgadmin@example.test"
    assert runtime.frontend_host == "127.0.0.1"
    assert runtime.frontend_port == "3001"
    assert runtime.frontend_api_url == "/api"
    assert bootstrap.super_admin_email == "superadmin@test.com"
    assert bootstrap.organization_admin_email == runtime.allowed_organization_admin
    assert bootstrap.runtime_user_email == "runtime@example.test"
    assert bootstrap.role_external_id == "ifactory-pdm-runtime"

    with pytest.raises(ValueError):
        replace(runtime, compose_project="overridden")
    with pytest.raises(ValueError):
        replace(bootstrap, super_admin_email="overridden@example.test")
    with pytest.raises(ValueError):
        replace(bootstrap, role_external_id="overridden")


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate",
        "unknown",
        "export",
        "interpolation",
        "nul",
        "multiline",
    ],
)
def test_env_parser_rejects_nonliteral_or_nonexact_runtime_env(
    tmp_path: Path,
    mutation: str,
) -> None:
    files = _write_config_tree(tmp_path)
    path = files["runtime"]
    data = path.read_bytes()
    if mutation == "duplicate":
        data += b"POSTGRES_DB=atlas\n"
    elif mutation == "unknown":
        data += b"PUBLIC_API_URL=http://unsafe.invalid\n"
    elif mutation == "export":
        data = data.replace(b"POSTGRES_DB=atlas", b"export POSTGRES_DB=atlas")
    elif mutation == "interpolation":
        data = data.replace(b"POSTGRES_USER=atlas_user", b"POSTGRES_USER=${USER}")
    elif mutation == "nul":
        data = data.replace(b"POSTGRES_DB=atlas", b"POSTGRES_DB=atlas\x00bad")
    else:
        data = data.replace(b"POSTGRES_USER=atlas_user", b"POSTGRES_USER=atlas\nuser")
    path.write_bytes(data)
    path.chmod(0o600)

    with pytest.raises(DeploymentError) as caught:
        RuntimeConfig.load(tmp_path)

    assert caught.value.code == "CMMS-E002"


def test_config_does_not_fall_back_to_inherited_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = _write_config_tree(tmp_path)
    _delete_env_key(files["runtime"], "POSTGRES_USER")
    monkeypatch.setenv("POSTGRES_USER", "ambient_user")

    with pytest.raises(DeploymentError) as caught:
        RuntimeConfig.load(tmp_path)

    assert caught.value.code == "CMMS-E002"


@pytest.mark.parametrize(
    ("file_name", "key", "value"),
    [
        ("frontend", "HOST", "0.0.0.0"),
        ("frontend", "PORT", "3000"),
        ("frontend", "API_URL", "http://unsafe.invalid"),
        ("runtime", "POSTGRES_DB", "other"),
        ("bootstrap", "ROLE_EXTERNAL_ID", "other-role"),
        ("bootstrap", "API_KEY_LABEL", "other-key"),
    ],
)
def test_config_rejects_changes_to_fixed_env_values(
    tmp_path: Path,
    file_name: str,
    key: str,
    value: str,
) -> None:
    files = _write_config_tree(tmp_path)
    _replace_env_value(files[file_name], key, value)

    with pytest.raises(DeploymentError) as caught:
        (RuntimeConfig if file_name != "bootstrap" else BootstrapConfig).load(tmp_path)

    assert caught.value.code == "CMMS-E002"


@pytest.mark.parametrize(
    ("mode", "license_file"),
    [("offline", ""), ("online", "/absolute/unapproved"), ("invalid", "")],
)
def test_runtime_license_mode_and_file_combinations_are_closed(
    tmp_path: Path,
    mode: str,
    license_file: str,
) -> None:
    files = _write_config_tree(tmp_path, mode="online")
    _replace_env_value(files["runtime"], "LICENSE_MODE", mode)
    _replace_env_value(files["runtime"], "LICENSE_FILE_PATH", license_file)

    with pytest.raises(DeploymentError) as caught:
        RuntimeConfig.load(tmp_path)

    assert caught.value.code == "CMMS-E002"


@pytest.mark.parametrize(
    "value",
    [
        "relative-secret",
        "/tmp/outside-secret",
        "/tmp/../tmp/outside-secret",
    ],
)
def test_runtime_rejects_noncanonical_or_unapproved_secret_references(
    tmp_path: Path,
    value: str,
) -> None:
    files = _write_config_tree(tmp_path)
    _replace_env_value(files["runtime"], "JWT_SECRET_KEY_FILE", value)

    with pytest.raises(DeploymentError) as caught:
        RuntimeConfig.load(tmp_path)

    assert caught.value.code == "CMMS-E002"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("ALLOWED_ORGANIZATION_ADMINS", ""),
        ("ALLOWED_ORGANIZATION_ADMINS", "one@example.test,two@example.test"),
        ("ALLOWED_ORGANIZATION_ADMINS", "OrgAdmin@example.test"),
        ("ALLOWED_ORGANIZATION_ADMINS", " orgadmin@example.test"),
    ],
)
def test_runtime_requires_one_canonical_organization_admin(
    tmp_path: Path,
    key: str,
    value: str,
) -> None:
    files = _write_config_tree(tmp_path)
    _replace_env_value(files["runtime"], key, value)

    with pytest.raises(DeploymentError) as caught:
        RuntimeConfig.load(tmp_path)

    assert caught.value.code == "CMMS-E002"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("ORGANIZATION_ADMIN_EMAIL", "different@example.test"),
        ("ORGANIZATION_ADMIN_EMAIL", "OrgAdmin@example.test"),
        ("RUNTIME_USER_EMAIL", "orgadmin@example.test"),
        ("RUNTIME_USER_EMAIL", "superadmin@test.com"),
        ("RUNTIME_USER_EMAIL", " runtime@example.test"),
        ("RUNTIME_USER_EMAIL", "runtimé@example.test"),
    ],
)
def test_bootstrap_rejects_identity_drift_collision_or_noncanonical_email(
    tmp_path: Path,
    key: str,
    value: str,
) -> None:
    files = _write_config_tree(tmp_path)
    _replace_env_value(files["bootstrap"], key, value)

    with pytest.raises(DeploymentError) as caught:
        BootstrapConfig.load(tmp_path)

    assert caught.value.code == "CMMS-E002"


def test_online_runtime_config_accepts_present_empty_license_file_key(tmp_path: Path) -> None:
    _write_config_tree(tmp_path, mode="online")

    loaded = RuntimeConfig.load(tmp_path)

    assert loaded.license_mode == "online"
    assert loaded.license_file_path is None


def test_canonical_json_has_sorted_compact_utf8_and_one_newline() -> None:
    assert canonical_json_bytes({"z": "测试", "a": [True, None, 3]}) == (
        b'{"a":[true,null,3],"z":"\xe6\xb5\x8b\xe8\xaf\x95"}\n'
    )


def test_canonical_json_rejects_duplicate_keys() -> None:
    payload = b'{"schema_version":1,"schema_version":1}\n'
    with pytest.raises(DeploymentError) as caught:
        strict_canonical_json_loads(payload, max_bytes=1024, max_depth=8)
    assert caught.value.code == "CMMS-E011"


@pytest.mark.parametrize(
    "payload",
    [
        b'{"value":1.5}\n',
        b'{"value":NaN}\n',
        b'{"value":Infinity}\n',
        b'{ "value":1}\n',
        b'{"value":1}',
        b'\xff\n',
    ],
)
def test_strict_canonical_json_rejects_floats_constants_encoding_and_bytes(
    payload: bytes,
) -> None:
    with pytest.raises(DeploymentError) as caught:
        strict_canonical_json_loads(payload, max_bytes=1024, max_depth=8)
    assert caught.value.code == "CMMS-E011"


def test_strict_canonical_json_rejects_excessive_size_and_depth() -> None:
    with pytest.raises(DeploymentError):
        strict_canonical_json_loads(b'{"value":"12345"}\n', max_bytes=8, max_depth=8)
    with pytest.raises(DeploymentError):
        strict_canonical_json_loads(b'{"a":{"b":{"c":1}}}\n', max_bytes=128, max_depth=2)


@pytest.mark.parametrize(
    "value",
    [
        {"password": "redacted"},
        {"nested": {"service-token": "redacted"}},
        {"safe": SENTINEL_SECRET},
        {"safe": "Bearer redacted"},
        {"safe": "url?X-Amz-Signature=redacted"},
    ],
)
def test_canonical_json_rejects_frozen_secret_keys_and_values(value: object) -> None:
    with pytest.raises(DeploymentError) as caught:
        canonical_json_bytes(value)  # type: ignore[arg-type]
    assert caught.value.code == "CMMS-E011"


def test_canonical_json_allows_explicit_nonsecret_api_key_and_license_metadata() -> None:
    legal = {
        "api_key_id": 42,
        "api_key_label": "ifactory-pdm-runtime",
        "api_key_capture_attempt_id": "a" * 32,
        "api_key_capture_status": "ANCHORED_RAW",
        "api_key_capture_file": None,
        "api_key_capture_cleared": True,
        "revoked_api_key_ids": [41],
        "license_mode": "offline",
        "license_guard_generation": "1" * 64,
    }

    assert strict_canonical_json_loads(
        canonical_json_bytes(legal),
        max_bytes=4096,
        max_depth=8,
    ) == legal


def _action(
    code: ActionCode,
    kind: ActionTargetKind | None = None,
    target: str | None = None,
) -> PlannedAction:
    return PlannedAction(code=code, target_kind=kind, target_id=target)


def _phase2_stat(seed: int = 900) -> SecureFileStatBinding:
    return SafeRuntimeFixture.stat_binding("phase2:env", seed)


def _fresh_bootstrap_bindings(
    safe_runtime: SafeRuntimeFixture,
) -> BootstrapPlanBindings:
    credentials = tuple(
        replace(
            row,
            candidate_file=safe_runtime.stat_binding(
                f"credential:{row.identity.value}:candidate",
                500 + index,
            ),
        )
        for index, row in enumerate(safe_runtime.bootstrap_plan_bindings.credentials or ())
    )
    return replace(
        safe_runtime.bootstrap_plan_bindings,
        credentials=credentials,
        invitation_probe=InvitationProbePlanBinding(
            slot_id=PROBE_SLOT,
            canonical_email="probe@example.test",
            descriptor_file=safe_runtime.stat_binding("probe:descriptor", 601),
            password_file=safe_runtime.stat_binding("probe:password", 602),
        ),
    )


def _capture_binding(safe_runtime: SafeRuntimeFixture) -> ApiKeyCapturePlanBinding:
    return ApiKeyCapturePlanBinding(
        attempt_id="d" * 32,
        api_key_id=42,
        label=ROLE_TARGET,
        runtime_user_id=1001,
        company_id=2001,
        captured_file=safe_runtime.stat_binding("api-key:capture", 701),
    )


def _cleanup_bindings(
    safe_runtime: SafeRuntimeFixture,
    outcome: ApiKeyCleanupOutcome,
) -> BootstrapPlanBindings:
    phase2 = _phase2_stat()
    cleanup = ApiKeyCleanupPlanBinding(
        attempt_id="e" * 32,
        api_key_id=42,
        terminal_outcome=outcome,
        historical_captured_file=safe_runtime.stat_binding("api-key:capture", 702),
        observed_captured_file=None,
        phase2_env_file=phase2 if outcome is ApiKeyCleanupOutcome.PUBLISHED else None,
    )
    return BootstrapPlanBindings(
        credentials=None,
        invitation_probe=None,
        api_key_capture=None,
        api_key_cleanup=cleanup,
        role_external_id=ROLE_TARGET,
        api_key_label=ROLE_TARGET,
        phase2_env_logical_id=PHASE2_TARGET,
        phase2_env_file=phase2 if outcome is ApiKeyCleanupOutcome.PUBLISHED else None,
    )


def _p_actions() -> tuple[PlannedAction, ...]:
    return (
        _action(ActionCode.READINESS_REQUIRE_LOOPBACK),
        _action(ActionCode.GATEWAY_ENABLE_DUAL),
        _action(ActionCode.READINESS_REQUIRE_DUAL),
    )


def _n_actions(mode: LicenseMode) -> tuple[PlannedAction, ...]:
    license_action = (
        ActionCode.LICENSE_VERIFY_OFFLINE
        if mode is LicenseMode.OFFLINE
        else ActionCode.LICENSE_DEBIT_ONLINE_START
    )
    return (
        _action(license_action),
        _action(ActionCode.PROCESS_CREATE_API_PERMIT),
        _action(ActionCode.PROCESS_START_API),
    )


def _c_full() -> tuple[PlannedAction, ...]:
    return (
        _action(
            ActionCode.BOOTSTRAP_ROTATE_SUPER_ADMIN,
            ActionTargetKind.IDENTITY,
            CredentialIdentity.SUPER_ADMIN.value,
        ),
        _action(
            ActionCode.BOOTSTRAP_CREATE_ORGANIZATION,
            ActionTargetKind.IDENTITY,
            CredentialIdentity.ORGANIZATION_ADMIN.value,
        ),
        _action(
            ActionCode.BOOTSTRAP_CREATE_ROLE,
            ActionTargetKind.ROLE_EXTERNAL_ID,
            ROLE_TARGET,
        ),
        _action(
            ActionCode.BOOTSTRAP_PROBE_INVITATION,
            ActionTargetKind.INVITATION_PROBE_SLOT,
            PROBE_SLOT,
        ),
        _action(
            ActionCode.BOOTSTRAP_CREATE_INVITATION,
            ActionTargetKind.IDENTITY,
            CredentialIdentity.RUNTIME_USER.value,
        ),
        _action(
            ActionCode.BOOTSTRAP_CREATE_RUNTIME_IDENTITY,
            ActionTargetKind.IDENTITY,
            CredentialIdentity.RUNTIME_USER.value,
        ),
        _action(
            ActionCode.BOOTSTRAP_CREATE_API_KEY,
            ActionTargetKind.API_KEY_LABEL,
            ROLE_TARGET,
        ),
        _action(
            ActionCode.BOOTSTRAP_FINALIZE_ROLE,
            ActionTargetKind.ROLE_EXTERNAL_ID,
            ROLE_TARGET,
        ),
        _action(
            ActionCode.BOOTSTRAP_PUBLISH_PHASE2_KEY,
            ActionTargetKind.PHASE2_ENV,
            PHASE2_TARGET,
        ),
    )


def _legal_branch(
    branch: str,
    mode: LicenseMode,
) -> tuple[PlannedAction, ...]:
    f = (_action(ActionCode.GATEWAY_FAIL_CLOSED),)
    n = _n_actions(mode)
    p = _p_actions()
    if branch == "start-active":
        return f + p
    if branch == "start-stopped":
        return (
            *f,
            _action(
                ActionCode.COMPOSE_START_STATE_GATEWAY,
                ActionTargetKind.COMPOSE_RESOURCE_SET,
                COMPOSE_TARGET,
            ),
            *n,
            _action(ActionCode.PROCESS_START_FRONTEND),
            *p,
        )
    if branch == "restart-api":
        return (
            *f,
            _action(ActionCode.PROCESS_STOP_API),
            _action(ActionCode.BUILD_API),
            *n,
            *p,
        )
    if branch == "restart-frontend":
        return (
            *f,
            _action(ActionCode.PROCESS_STOP_FRONTEND),
            _action(ActionCode.PROCESS_START_FRONTEND),
            *p,
        )
    if branch == "stop":
        return (
            *f,
            _action(ActionCode.PROCESS_STOP_FRONTEND),
            _action(ActionCode.PROCESS_STOP_API),
            _action(
                ActionCode.COMPOSE_STOP_STATE_GATEWAY,
                ActionTargetKind.COMPOSE_RESOURCE_SET,
                COMPOSE_TARGET,
            ),
        )
    if branch == "bootstrap":
        return (
            *f,
            _action(
                ActionCode.COMPOSE_CREATE_STATE_GATEWAY,
                ActionTargetKind.COMPOSE_RESOURCE_SET,
                COMPOSE_TARGET,
            ),
            *n,
            _action(ActionCode.CMMS_INITIALIZE_FRESH_DATABASE),
            _action(ActionCode.PROCESS_START_FRONTEND),
            _action(ActionCode.READINESS_REQUIRE_API_LOOPBACK),
            *_c_full(),
            *p,
        )
    if branch == "repair-discovery":
        return (
            *f,
            *n,
            _action(ActionCode.READINESS_REQUIRE_API_LOOPBACK),
            _action(
                ActionCode.REPAIR_CAPTURE_BOOTSTRAP_DISCOVERY,
                ActionTargetKind.RECEIPT,
                RECEIPT_TARGET,
            ),
            _action(ActionCode.REPAIR_STOP_LOOPBACK_RUNTIME),
        )
    if branch == "repair-completion":
        return (
            *f,
            *n,
            _action(ActionCode.PROCESS_START_FRONTEND),
            _action(ActionCode.READINESS_REQUIRE_API_LOOPBACK),
            *_c_full(),
            *p,
        )
    if branch == "repair-readiness":
        return (
            *f,
            *n,
            _action(ActionCode.PROCESS_START_FRONTEND),
            _action(ActionCode.READINESS_REQUIRE_API_LOOPBACK),
            *p,
        )
    if branch == "repair-revoke":
        return (
            *f,
            *n,
            _action(ActionCode.READINESS_REQUIRE_API_LOOPBACK),
            _action(
                ActionCode.REPAIR_REVOKE_UNCAPTURED_API_KEY,
                ActionTargetKind.API_KEY_ID,
                "42",
            ),
            _action(ActionCode.REPAIR_STOP_LOOPBACK_RUNTIME),
        )
    if branch == "repair-discard":
        return (
            *f,
            *n,
            _action(ActionCode.READINESS_REQUIRE_API_LOOPBACK),
            _action(
                ActionCode.REPAIR_DISCARD_REJECTED_CANDIDATE,
                ActionTargetKind.IDENTITY,
                CredentialIdentity.RUNTIME_USER.value,
            ),
            _action(ActionCode.REPAIR_STOP_LOOPBACK_RUNTIME),
        )
    if branch == "repair-budget":
        return (
            *f,
            _action(ActionCode.LICENSE_RECOVER_UNKNOWN_BUDGET),
            _action(ActionCode.PROCESS_CREATE_API_PERMIT),
            _action(ActionCode.PROCESS_START_API),
            _action(ActionCode.PROCESS_START_FRONTEND),
            *p,
        )
    if branch == "repair-cleanup":
        return (
            *f,
            _action(
                ActionCode.REPAIR_FINALIZE_API_KEY_CAPTURE_CLEANUP,
                ActionTargetKind.RECEIPT,
                RECEIPT_TARGET,
            ),
        )
    if branch == "switch-license":
        return (
            *f,
            _action(ActionCode.PROCESS_STOP_API),
            _action(ActionCode.LICENSE_SWITCH_MODE),
            *n,
            *p,
        )
    raise AssertionError(branch)


BRANCH_CASES = (
    ("start-active", Operation.START, LicenseMode.OFFLINE),
    ("start-stopped", Operation.START, LicenseMode.ONLINE),
    ("restart-api", Operation.RESTART_API, LicenseMode.ONLINE),
    ("restart-frontend", Operation.RESTART_FRONTEND, LicenseMode.OFFLINE),
    ("stop", Operation.STOP, LicenseMode.OFFLINE),
    ("bootstrap", Operation.BOOTSTRAP, LicenseMode.OFFLINE),
    ("repair-discovery", Operation.REPAIR, LicenseMode.OFFLINE),
    ("repair-completion", Operation.REPAIR, LicenseMode.OFFLINE),
    ("repair-readiness", Operation.REPAIR, LicenseMode.ONLINE),
    ("repair-revoke", Operation.REPAIR, LicenseMode.OFFLINE),
    ("repair-discard", Operation.REPAIR, LicenseMode.ONLINE),
    ("repair-budget", Operation.REPAIR, LicenseMode.ONLINE),
    ("repair-cleanup", Operation.REPAIR, LicenseMode.OFFLINE),
    ("switch-license", Operation.SWITCH_LICENSE, LicenseMode.ONLINE),
)


def _bindings_for_branch(
    safe_runtime: SafeRuntimeFixture,
    branch: str,
) -> BootstrapPlanBindings | None:
    if branch == "stop":
        return None
    if branch == "bootstrap" or branch == "repair-completion":
        return _fresh_bootstrap_bindings(safe_runtime)
    if branch == "repair-discard":
        credentials = tuple(
            replace(
                row,
                candidate_file=(
                    safe_runtime.stat_binding("credential:runtime-user:candidate", 799)
                    if row.identity is CredentialIdentity.RUNTIME_USER
                    else row.candidate_file
                ),
            )
            for row in safe_runtime.bootstrap_plan_bindings.credentials or ()
        )
        return replace(
            safe_runtime.bootstrap_plan_bindings,
            credentials=credentials,
        )
    if branch == "repair-cleanup":
        return _cleanup_bindings(safe_runtime, ApiKeyCleanupOutcome.PUBLISHED)
    return safe_runtime.bootstrap_plan_bindings


def test_registry_rank_contains_each_action_exactly_once() -> None:
    assert len(ACTION_RANK) == 40
    assert set(ACTION_RANK) == set(ActionCode)
    assert len(set(ACTION_RANK)) == len(ACTION_RANK)
    assert ACTION_RANK[0] is ActionCode.GATEWAY_FAIL_CLOSED
    assert ACTION_RANK[-1] is ActionCode.REPAIR_FINALIZE_API_KEY_CAPTURE_CLEANUP


FROZEN_ACTION_RANK_VALUES = (
    "gateway.fail-closed",
    "process.stop-frontend",
    "process.stop-api",
    "license.stop-guard",
    "compose.stop-state-gateway",
    "runtime.install-control",
    "toolchain.install",
    "images.pull-exact",
    "compose.create-state-gateway",
    "compose.start-state-gateway",
    "build.api",
    "frontend.verify",
    "systemd.install-units",
    "license.switch-mode",
    "license.verify-offline",
    "license.debit-online-start",
    "license.recover-unknown-budget",
    "process.create-api-permit",
    "process.start-api",
    "cmms.initialize-fresh-database",
    "process.start-frontend",
    "readiness.require-api-loopback",
    "repair.capture-bootstrap-discovery",
    "repair.revoke-uncaptured-api-key",
    "repair.discard-rejected-candidate",
    "bootstrap.rotate-super-admin",
    "bootstrap.create-organization",
    "bootstrap.create-role",
    "bootstrap.probe-invitation-enforcement",
    "bootstrap.create-invitation",
    "bootstrap.create-runtime-identity",
    "bootstrap.create-api-key",
    "bootstrap.finalize-role",
    "bootstrap.publish-phase2-api-key",
    "repair.stop-loopback-runtime",
    "readiness.require-loopback",
    "gateway.enable-dual",
    "readiness.require-dual",
    "readiness.probe-minio-route",
    "repair.finalize-api-key-capture-cleanup",
)

FROZEN_ALLOWED_OPERATIONS = {
    "gateway.fail-closed": {"bootstrap", "start", "restart-api", "restart-frontend", "stop", "repair", "switch-license"},
    "process.stop-frontend": {"restart-frontend", "stop"},
    "process.stop-api": {"restart-api", "stop", "switch-license"},
    "license.stop-guard": {"restart-api", "stop", "switch-license"},
    "compose.stop-state-gateway": {"stop"},
    "runtime.install-control": {"bootstrap", "repair"},
    "toolchain.install": {"bootstrap", "repair"},
    "images.pull-exact": {"bootstrap", "repair"},
    "compose.create-state-gateway": {"bootstrap"},
    "compose.start-state-gateway": {"start", "repair"},
    "build.api": {"bootstrap", "restart-api", "repair"},
    "frontend.verify": {"bootstrap", "repair"},
    "systemd.install-units": {"bootstrap", "repair"},
    "license.switch-mode": {"switch-license"},
    "license.verify-offline": {"bootstrap", "start", "restart-api", "repair", "switch-license"},
    "license.debit-online-start": {"bootstrap", "start", "restart-api", "repair", "switch-license"},
    "license.recover-unknown-budget": {"repair"},
    "process.create-api-permit": {"bootstrap", "start", "restart-api", "repair", "switch-license"},
    "process.start-api": {"bootstrap", "start", "restart-api", "repair", "switch-license"},
    "cmms.initialize-fresh-database": {"bootstrap"},
    "process.start-frontend": {"bootstrap", "start", "restart-frontend", "repair", "switch-license"},
    "readiness.require-api-loopback": {"bootstrap", "repair"},
    "repair.capture-bootstrap-discovery": {"repair"},
    "repair.revoke-uncaptured-api-key": {"repair"},
    "repair.discard-rejected-candidate": {"repair"},
    "bootstrap.rotate-super-admin": {"bootstrap", "repair"},
    "bootstrap.create-organization": {"bootstrap", "repair"},
    "bootstrap.create-role": {"bootstrap", "repair"},
    "bootstrap.probe-invitation-enforcement": {"bootstrap", "repair"},
    "bootstrap.create-invitation": {"bootstrap", "repair"},
    "bootstrap.create-runtime-identity": {"bootstrap", "repair"},
    "bootstrap.create-api-key": {"bootstrap", "repair"},
    "bootstrap.finalize-role": {"bootstrap", "repair"},
    "bootstrap.publish-phase2-api-key": {"bootstrap", "repair"},
    "repair.stop-loopback-runtime": {"repair"},
    "readiness.require-loopback": {"bootstrap", "start", "restart-api", "restart-frontend", "repair", "switch-license"},
    "gateway.enable-dual": {"bootstrap", "start", "restart-api", "restart-frontend", "repair", "switch-license"},
    "readiness.require-dual": {"bootstrap", "start", "restart-api", "restart-frontend", "repair", "switch-license"},
    "readiness.probe-minio-route": {"bootstrap", "repair"},
    "repair.finalize-api-key-capture-cleanup": {"repair"},
}

FROZEN_REQUIRED_TARGET_ACTIONS = {
    "compose.create-state-gateway",
    "compose.start-state-gateway",
    "compose.stop-state-gateway",
    "repair.capture-bootstrap-discovery",
    "repair.revoke-uncaptured-api-key",
    "repair.discard-rejected-candidate",
    "bootstrap.rotate-super-admin",
    "bootstrap.create-organization",
    "bootstrap.create-role",
    "bootstrap.probe-invitation-enforcement",
    "bootstrap.create-invitation",
    "bootstrap.create-runtime-identity",
    "bootstrap.create-api-key",
    "bootstrap.finalize-role",
    "bootstrap.publish-phase2-api-key",
    "repair.finalize-api-key-capture-cleanup",
}


def test_registry_rank_and_operation_policies_match_frozen_literal_contract() -> None:
    assert tuple(code.value for code in ACTION_RANK) == FROZEN_ACTION_RANK_VALUES
    assert {
        code.value: {operation.value for operation in ActionRegistry.definition(code).allowed_operations}
        for code in ActionCode
    } == FROZEN_ALLOWED_OPERATIONS
    assert {
        code.value: ActionRegistry.definition(code).allows_multiple_targets
        for code in ActionCode
    } == {value: False for value in FROZEN_ACTION_RANK_VALUES}
    assert {
        code.value: ActionRegistry.definition(code).target_policy
        for code in ActionCode
    } == {
        value: "required" if value in FROZEN_REQUIRED_TARGET_ACTIONS else "forbidden"
        for value in FROZEN_ACTION_RANK_VALUES
    }


def test_registry_single_target_metadata_rejects_both_secondary_ascii_orders(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    base = _legal_branch("repair-revoke", LicenseMode.OFFLINE)
    target_index = next(
        index
        for index, row in enumerate(base)
        if row.code is ActionCode.REPAIR_REVOKE_UNCAPTURED_API_KEY
    )
    repeated = (
        _action(
            ActionCode.REPAIR_REVOKE_UNCAPTURED_API_KEY,
            ActionTargetKind.API_KEY_ID,
            "41",
        ),
        _action(
            ActionCode.REPAIR_REVOKE_UNCAPTURED_API_KEY,
            ActionTargetKind.API_KEY_ID,
            "42",
        ),
    )
    for targets in (repeated, tuple(reversed(repeated))):
        actions = base[:target_index] + targets + base[target_index + 1 :]
        with pytest.raises(DeploymentError):
            ActionRegistry.validate(
                Operation.REPAIR,
                RuntimeProfile.DEVELOPMENT,
                LicenseMode.OFFLINE,
                safe_runtime.bootstrap_plan_bindings,
                actions,
            )


def test_registry_has_one_complete_immutable_definition_per_action() -> None:
    for code in ActionCode:
        definition = ActionRegistry.definition(code)
        assert definition.handler
        assert definition.mutation_class
        assert isinstance(definition.allowed_operations, frozenset)
        assert definition.allowed_operations
        assert definition.target_policy


@pytest.mark.parametrize(("branch", "operation", "mode"), BRANCH_CASES)
def test_registry_accepts_each_closed_branch_grammar(
    safe_runtime: SafeRuntimeFixture,
    branch: str,
    operation: Operation,
    mode: LicenseMode,
) -> None:
    ActionRegistry.validate(
        operation,
        RuntimeProfile.DEVELOPMENT,
        mode,
        _bindings_for_branch(safe_runtime, branch),
        _legal_branch(branch, mode),
    )


@pytest.mark.parametrize(("branch", "operation", "mode"), BRANCH_CASES)
def test_registry_rejects_one_missing_required_action_from_each_branch(
    safe_runtime: SafeRuntimeFixture,
    branch: str,
    operation: Operation,
    mode: LicenseMode,
) -> None:
    actions = _legal_branch(branch, mode)
    remove_at = 1
    invalid = actions[:remove_at] + actions[remove_at + 1 :]

    with pytest.raises(DeploymentError) as caught:
        ActionRegistry.validate(
            operation,
            RuntimeProfile.DEVELOPMENT,
            mode,
            _bindings_for_branch(safe_runtime, branch),
            invalid,
        )

    assert caught.value.code == "CMMS-E011"


def test_registry_rejects_duplicate_alternative_order_and_cross_class_mix(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    valid = _legal_branch("start-stopped", LicenseMode.OFFLINE)
    invalid_rows = (
        valid + (valid[-1],),
        (valid[0], valid[2], valid[1], *valid[3:]),
        valid + (
            _action(
                ActionCode.REPAIR_REVOKE_UNCAPTURED_API_KEY,
                ActionTargetKind.API_KEY_ID,
                "42",
            ),
        ),
    )
    for invalid in invalid_rows:
        with pytest.raises(DeploymentError):
            ActionRegistry.validate(
                Operation.START,
                RuntimeProfile.DEVELOPMENT,
                LicenseMode.OFFLINE,
                safe_runtime.bootstrap_plan_bindings,
                invalid,
            )


@pytest.mark.parametrize(
    "target",
    ["0", "-1", "+1", "01", "9223372036854775808", "not-an-id"],
)
def test_registry_rejects_noncanonical_java_long_api_key_target(
    safe_runtime: SafeRuntimeFixture,
    target: str,
) -> None:
    actions = list(_legal_branch("repair-revoke", LicenseMode.OFFLINE))
    index = next(
        i
        for i, row in enumerate(actions)
        if row.code is ActionCode.REPAIR_REVOKE_UNCAPTURED_API_KEY
    )
    actions[index] = replace(actions[index], target_id=target)

    with pytest.raises(DeploymentError):
        ActionRegistry.validate(
            Operation.REPAIR,
            RuntimeProfile.DEVELOPMENT,
            LicenseMode.OFFLINE,
            safe_runtime.bootstrap_plan_bindings,
            actions,
        )


def test_registry_rejects_profile_license_and_binding_mismatch(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    offline = _legal_branch("start-stopped", LicenseMode.OFFLINE)
    with pytest.raises(DeploymentError):
        ActionRegistry.validate(
            Operation.START,
            RuntimeProfile.DEVELOPMENT,
            LicenseMode.ONLINE,
            safe_runtime.bootstrap_plan_bindings,
            offline,
        )

    bootstrap = _legal_branch("bootstrap", LicenseMode.OFFLINE)
    wrong = replace(
        _fresh_bootstrap_bindings(safe_runtime),
        role_external_id="different",
    )
    with pytest.raises(DeploymentError):
        ActionRegistry.validate(
            Operation.BOOTSTRAP,
            RuntimeProfile.DEVELOPMENT,
            LicenseMode.OFFLINE,
            wrong,
            bootstrap,
        )


def test_registry_requires_contiguous_completion_suffix_and_anchored_capture(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    prefix = (
        _action(ActionCode.GATEWAY_FAIL_CLOSED),
        *_n_actions(LicenseMode.OFFLINE),
        _action(ActionCode.PROCESS_START_FRONTEND),
        _action(ActionCode.READINESS_REQUIRE_API_LOOPBACK),
    )
    publish = _c_full()[-1:]
    actions = prefix + publish + _p_actions()
    bindings = replace(
        safe_runtime.bootstrap_plan_bindings,
        api_key_capture=_capture_binding(safe_runtime),
    )
    ActionRegistry.validate(
        Operation.REPAIR,
        RuntimeProfile.DEVELOPMENT,
        LicenseMode.OFFLINE,
        bindings,
        actions,
    )

    with pytest.raises(DeploymentError):
        ActionRegistry.validate(
            Operation.REPAIR,
            RuntimeProfile.DEVELOPMENT,
            LicenseMode.OFFLINE,
            safe_runtime.bootstrap_plan_bindings,
            actions,
        )
    with pytest.raises(DeploymentError):
        ActionRegistry.validate(
            Operation.REPAIR,
            RuntimeProfile.DEVELOPMENT,
            LicenseMode.OFFLINE,
            bindings,
            prefix + (_c_full()[0], _c_full()[-1]) + _p_actions(),
        )


def test_cleanup_only_requires_credentials_null_and_exact_stat_projection(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    actions = _legal_branch("repair-cleanup", LicenseMode.OFFLINE)
    bindings = _cleanup_bindings(safe_runtime, ApiKeyCleanupOutcome.PUBLISHED)
    ActionRegistry.validate(
        Operation.REPAIR,
        RuntimeProfile.DEVELOPMENT,
        LicenseMode.OFFLINE,
        bindings,
        actions,
    )

    invalid = (
        replace(bindings, credentials=safe_runtime.bootstrap_plan_bindings.credentials),
        replace(bindings, phase2_env_file=_phase2_stat(901)),
        replace(
            bindings,
            api_key_cleanup=replace(
                bindings.api_key_cleanup,
                phase2_env_file=None,
            ),
        ),
    )
    for changed in invalid:
        with pytest.raises(DeploymentError):
            ActionRegistry.validate(
                Operation.REPAIR,
                RuntimeProfile.DEVELOPMENT,
                LicenseMode.OFFLINE,
                changed,
                actions,
            )


def test_revoked_cleanup_requires_both_phase2_stats_null(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    actions = _legal_branch("repair-cleanup", LicenseMode.OFFLINE)
    valid = _cleanup_bindings(safe_runtime, ApiKeyCleanupOutcome.REVOKED)
    ActionRegistry.validate(
        Operation.REPAIR,
        RuntimeProfile.DEVELOPMENT,
        LicenseMode.OFFLINE,
        valid,
        actions,
    )

    with pytest.raises(DeploymentError):
        ActionRegistry.validate(
            Operation.REPAIR,
            RuntimeProfile.DEVELOPMENT,
            LicenseMode.OFFLINE,
            replace(valid, phase2_env_file=_phase2_stat()),
            actions,
        )


def test_minio_probe_is_structurally_acceptance_only(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    actions = _legal_branch("bootstrap", LicenseMode.OFFLINE) + (
        _action(ActionCode.READINESS_PROBE_MINIO_ROUTE),
    )
    with pytest.raises(DeploymentError):
        ActionRegistry.validate(
            Operation.BOOTSTRAP,
            RuntimeProfile.DEVELOPMENT,
            LicenseMode.OFFLINE,
            _fresh_bootstrap_bindings(safe_runtime),
            actions,
        )


_FROZEN_RANK_INDEX = {
    value: index for index, value in enumerate(FROZEN_ACTION_RANK_VALUES)
}


def _frozen_order(actions: tuple[PlannedAction, ...]) -> tuple[PlannedAction, ...]:
    return tuple(
        sorted(
            actions,
            key=lambda row: (
                _FROZEN_RANK_INDEX[row.code.value],
                "" if row.target_kind is None else row.target_kind.value,
                "" if row.target_id is None else row.target_id,
            ),
        )
    )


def _all_action_subsets(
    actions: tuple[PlannedAction, ...],
) -> tuple[tuple[PlannedAction, ...], ...]:
    return tuple(
        subset
        for size in range(len(actions) + 1)
        for subset in itertools.combinations(actions, size)
    )


def _repair_completion_bindings(
    safe_runtime: SafeRuntimeFixture,
    suffix: tuple[PlannedAction, ...],
) -> BootstrapPlanBindings:
    bindings = _fresh_bootstrap_bindings(safe_runtime)
    has_probe = any(
        row.code is ActionCode.BOOTSTRAP_PROBE_INVITATION for row in suffix
    )
    has_create_key = any(
        row.code is ActionCode.BOOTSTRAP_CREATE_API_KEY for row in suffix
    )
    return replace(
        bindings,
        invitation_probe=bindings.invitation_probe if has_probe else None,
        api_key_capture=None if has_create_key else _capture_binding(safe_runtime),
    )


def test_registry_accepts_all_optional_infrastructure_families(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    bootstrap_optional = tuple(
        _action(code)
        for code in (
            ActionCode.RUNTIME_INSTALL_CONTROL,
            ActionCode.TOOLCHAIN_INSTALL,
            ActionCode.IMAGES_PULL_EXACT,
            ActionCode.BUILD_API,
            ActionCode.FRONTEND_VERIFY,
            ActionCode.SYSTEMD_INSTALL_UNITS,
        )
    )
    for optional in _all_action_subsets(bootstrap_optional):
        ActionRegistry.validate(
            Operation.BOOTSTRAP,
            RuntimeProfile.DEVELOPMENT,
            LicenseMode.OFFLINE,
            _fresh_bootstrap_bindings(safe_runtime),
            _frozen_order(
                _legal_branch("bootstrap", LicenseMode.OFFLINE) + optional
            ),
        )

    repair_api_optional = (
        _action(ActionCode.RUNTIME_INSTALL_CONTROL),
        _action(ActionCode.TOOLCHAIN_INSTALL),
        _action(ActionCode.IMAGES_PULL_EXACT),
        _action(
            ActionCode.COMPOSE_START_STATE_GATEWAY,
            ActionTargetKind.COMPOSE_RESOURCE_SET,
            COMPOSE_TARGET,
        ),
        _action(ActionCode.BUILD_API),
        _action(ActionCode.SYSTEMD_INSTALL_UNITS),
    )
    for optional in _all_action_subsets(repair_api_optional):
        ActionRegistry.validate(
            Operation.REPAIR,
            RuntimeProfile.DEVELOPMENT,
            LicenseMode.OFFLINE,
            safe_runtime.bootstrap_plan_bindings,
            _frozen_order(
                _legal_branch("repair-discovery", LicenseMode.OFFLINE)
                + optional
            ),
        )

    repair_full_optional = repair_api_optional + (
        _action(ActionCode.FRONTEND_VERIFY),
    )
    for optional in _all_action_subsets(repair_full_optional):
        for branch, mode in (
            ("repair-completion", LicenseMode.OFFLINE),
            ("repair-readiness", LicenseMode.ONLINE),
            ("repair-budget", LicenseMode.ONLINE),
        ):
            bindings = (
                _fresh_bootstrap_bindings(safe_runtime)
                if branch == "repair-completion"
                else safe_runtime.bootstrap_plan_bindings
            )
            ActionRegistry.validate(
                Operation.REPAIR,
                RuntimeProfile.DEVELOPMENT,
                mode,
                bindings,
                _frozen_order(_legal_branch(branch, mode) + optional),
            )


def test_registry_accepts_conditional_frontend_and_offline_guard_forms(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    start_active = _frozen_order(
        _legal_branch("start-active", LicenseMode.OFFLINE)
        + (_action(ActionCode.PROCESS_START_FRONTEND),)
    )
    ActionRegistry.validate(
        Operation.START,
        RuntimeProfile.DEVELOPMENT,
        LicenseMode.OFFLINE,
        safe_runtime.bootstrap_plan_bindings,
        start_active,
    )

    cases = (
        (Operation.RESTART_API, _legal_branch("restart-api", LicenseMode.OFFLINE)),
        (Operation.STOP, _legal_branch("stop", LicenseMode.OFFLINE)),
        (Operation.SWITCH_LICENSE, _legal_branch("switch-license", LicenseMode.ONLINE)),
    )
    for operation, base in cases:
        ActionRegistry.validate(
            operation,
            RuntimeProfile.DEVELOPMENT,
            LicenseMode.ONLINE if operation is Operation.SWITCH_LICENSE else LicenseMode.OFFLINE,
            None if operation is Operation.STOP else safe_runtime.bootstrap_plan_bindings,
            _frozen_order(base + (_action(ActionCode.LICENSE_STOP_GUARD),)),
        )

    ActionRegistry.validate(
        Operation.SWITCH_LICENSE,
        RuntimeProfile.DEVELOPMENT,
        LicenseMode.ONLINE,
        safe_runtime.bootstrap_plan_bindings,
        _frozen_order(
            _legal_branch("switch-license", LicenseMode.ONLINE)
            + (_action(ActionCode.PROCESS_START_FRONTEND),)
        ),
    )


def test_registry_accepts_every_contiguous_completion_suffix_language(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    full = _c_full()
    probe_resolved = tuple(
        row
        for row in full
        if row.code is not ActionCode.BOOTSTRAP_PROBE_INVITATION
    )
    prefix = (
        _action(ActionCode.GATEWAY_FAIL_CLOSED),
        *_n_actions(LicenseMode.OFFLINE),
        _action(ActionCode.PROCESS_START_FRONTEND),
        _action(ActionCode.READINESS_REQUIRE_API_LOOPBACK),
    )
    for language in (full, probe_resolved):
        for start in range(len(language)):
            suffix = language[start:]
            ActionRegistry.validate(
                Operation.REPAIR,
                RuntimeProfile.DEVELOPMENT,
                LicenseMode.OFFLINE,
                _repair_completion_bindings(safe_runtime, suffix),
                prefix + suffix + _p_actions(),
            )


def test_minio_is_only_allowed_for_acceptance_bootstrap_or_two_repair_forms(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    minio = (_action(ActionCode.READINESS_PROBE_MINIO_ROUTE),)
    legal = (
        (
            Operation.BOOTSTRAP,
            LicenseMode.OFFLINE,
            _fresh_bootstrap_bindings(safe_runtime),
            _legal_branch("bootstrap", LicenseMode.OFFLINE) + minio,
        ),
        (
            Operation.REPAIR,
            LicenseMode.OFFLINE,
            _fresh_bootstrap_bindings(safe_runtime),
            _legal_branch("repair-completion", LicenseMode.OFFLINE) + minio,
        ),
        (
            Operation.REPAIR,
            LicenseMode.ONLINE,
            safe_runtime.bootstrap_plan_bindings,
            _legal_branch("repair-readiness", LicenseMode.ONLINE) + minio,
        ),
    )
    for operation, mode, bindings, actions in legal:
        ActionRegistry.validate(
            operation,
            RuntimeProfile.ACCEPTANCE,
            mode,
            bindings,
            actions,
        )

    with pytest.raises(DeploymentError):
        ActionRegistry.validate(
            Operation.START,
            RuntimeProfile.ACCEPTANCE,
            LicenseMode.OFFLINE,
            safe_runtime.bootstrap_plan_bindings,
            _legal_branch("start-active", LicenseMode.OFFLINE) + minio,
        )


def test_fresh_bootstrap_requires_exact_frozen_binding_projection(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    actions = _legal_branch("bootstrap", LicenseMode.OFFLINE)
    valid = _fresh_bootstrap_bindings(safe_runtime)
    credentials = list(valid.credentials or ())
    credentials[0] = replace(credentials[0], candidate_file=None)
    invalid = (
        replace(valid, invitation_probe=None),
        replace(valid, api_key_capture=_capture_binding(safe_runtime)),
        replace(
            valid,
            api_key_cleanup=ApiKeyCleanupPlanBinding(
                attempt_id="e" * 32,
                api_key_id=42,
                terminal_outcome=ApiKeyCleanupOutcome.PUBLISHED,
                historical_captured_file=safe_runtime.stat_binding(
                    "api-key:capture",
                    780,
                ),
                observed_captured_file=None,
                phase2_env_file=valid.phase2_env_file,
            ),
        ),
        replace(valid, phase2_env_file=None),
        replace(valid, credentials=tuple(credentials)),
    )
    for bindings in invalid:
        with pytest.raises(DeploymentError):
            ActionRegistry.validate(
                Operation.BOOTSTRAP,
                RuntimeProfile.DEVELOPMENT,
                LicenseMode.OFFLINE,
                bindings,
                actions,
            )


def _rehash_plan_mapping(value: dict[str, object]) -> dict[str, object]:
    unsigned = copy.deepcopy(value)
    unsigned.pop("plan_sha256", None)
    value["plan_sha256"] = hashlib.sha256(
        canonical_json_bytes(unsigned)  # type: ignore[arg-type]
    ).hexdigest()
    return value


def test_plan_create_hashes_exact_30_minute_immutable_payload(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    plan = safe_runtime.make_plan()

    assert plan.expires_at - plan.created_at == timedelta(minutes=30)
    assert len(plan.plan_sha256) == 64
    assert plan.to_mapping()["plan_nonce"] == "a" * 32
    with pytest.raises(FrozenInstanceError):
        plan.plan_nonce = "b" * 32  # type: ignore[misc]


@pytest.mark.parametrize(
    "nonce",
    ["", "a" * 31, "A" * 32, "g" * 32, "a" * 33],
)
def test_plan_rejects_invalid_injected_nonce(
    safe_runtime: SafeRuntimeFixture,
    nonce: str,
) -> None:
    with pytest.raises(DeploymentError) as caught:
        safe_runtime.make_plan(plan_nonce=nonce)
    assert caught.value.code == "CMMS-E011"


def test_plan_create_requires_caller_injected_nonce_without_random_fallback(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    with patch(
        "ifactory_cmms_deploy.records.secrets.token_hex",
        side_effect=AssertionError("random nonce fallback must not run"),
    ):
        with pytest.raises(TypeError):
            DeploymentPlan.create(
                snapshot=safe_runtime.make_snapshot(),
                operation=Operation.START,
                profile=RuntimeProfile.DEVELOPMENT,
                license_mode=LicenseMode.OFFLINE,
                bootstrap_bindings=safe_runtime.bootstrap_plan_bindings,
                actions=_legal_branch("start-active", LicenseMode.OFFLINE),
                now=NOW,
            )


def test_distinct_injected_nonce_produces_distinct_plan_hash(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    first = safe_runtime.make_plan(plan_nonce="a" * 32)
    second = safe_runtime.make_plan(plan_nonce="b" * 32)

    assert first.plan_sha256 != second.plan_sha256


@pytest.mark.parametrize("mutation", ["root", "cmms", "gitlink"])
def test_acceptance_plan_rejects_uncommitted_or_detached_source(
    safe_runtime: SafeRuntimeFixture,
    mutation: str,
) -> None:
    snapshot = safe_runtime.make_snapshot()
    source = snapshot.source
    if mutation == "root":
        source = replace(source, root_status=SourceStatus.UNCOMMITTED)
    elif mutation == "cmms":
        source = replace(source, cmms_status=SourceStatus.UNCOMMITTED)
    else:
        source = replace(source, cmms_head="3" * 40)

    with pytest.raises(DeploymentError):
        DeploymentPlan.create(
            snapshot=replace(snapshot, source=source),
            operation=Operation.START,
            profile=RuntimeProfile.ACCEPTANCE,
            license_mode=LicenseMode.OFFLINE,
            bootstrap_bindings=safe_runtime.bootstrap_plan_bindings,
            actions=_legal_branch("start-active", LicenseMode.OFFLINE),
            now=NOW,
            plan_nonce="a" * 32,
        )


def test_stop_plan_serializes_null_bootstrap_bindings(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    plan = safe_runtime.make_plan(operation=Operation.STOP)
    assert plan.bootstrap_bindings is None
    assert plan.to_mapping()["bootstrap_bindings"] is None


def test_plan_constructor_itself_rejects_branch_invalid_actions(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    with pytest.raises(DeploymentError):
        DeploymentPlan.create(
            snapshot=safe_runtime.make_snapshot(),
            operation=Operation.START,
            profile=RuntimeProfile.DEVELOPMENT,
            license_mode=LicenseMode.OFFLINE,
            bootstrap_bindings=safe_runtime.bootstrap_plan_bindings,
            actions=(
                _action(ActionCode.GATEWAY_FAIL_CLOSED),
                _action(ActionCode.PROCESS_START_API),
            ),
            now=NOW,
            plan_nonce="a" * 32,
        )


def test_plan_loader_rejects_unknown_missing_nested_and_invalid_enum_fields(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    original = safe_runtime.make_plan().to_mapping()
    mutations = []
    missing = copy.deepcopy(original)
    del missing["profile"]
    mutations.append(missing)
    unknown = copy.deepcopy(original)
    unknown["unexpected"] = None
    mutations.append(unknown)
    nested = copy.deepcopy(original)
    nested["snapshot"]["source"]["unexpected"] = None  # type: ignore[index]
    mutations.append(nested)
    enum = copy.deepcopy(original)
    enum["operation"] = "unknown"
    mutations.append(enum)
    action = copy.deepcopy(original)
    del action["actions"][0]["target_id"]  # type: ignore[index]
    mutations.append(action)
    for value in mutations:
        with pytest.raises(DeploymentError):
            DeploymentPlan.from_mapping(_rehash_plan_mapping(value))


@pytest.mark.parametrize(
    ("generation", "sha"),
    [(0, "7" * 64), (1, None), (-1, None), (0, "")],
)
def test_plan_snapshot_rejects_noncanonical_absent_present_state_pairs(
    safe_runtime: SafeRuntimeFixture,
    generation: int,
    sha: str | None,
) -> None:
    with pytest.raises(DeploymentError):
        replace(
            safe_runtime.make_snapshot(),
            state_generation=generation,
            state_sha256=sha,
        )


def test_write_plan_uses_hash_filename_private_mode_and_never_overwrites(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    plan = safe_runtime.make_plan()
    path, digest = write_plan(plan, safe_runtime.plans_dir)

    assert path.name == f"{digest}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_bytes() == canonical_json_bytes(plan.to_mapping())
    with pytest.raises(DeploymentError):
        write_plan(plan, safe_runtime.plans_dir)


def test_static_plan_failures_create_no_application_record(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    plan = safe_runtime.make_plan()
    path, digest = write_plan(plan, safe_runtime.plans_dir)
    cases = (
        ("0" * 64, plan.created_at),
        (digest, plan.expires_at + timedelta(microseconds=1)),
    )
    for confirmed, now in cases:
        with pytest.raises(DeploymentError):
            reserve_plan_attempt(
                path,
                confirmed_sha256=confirmed,
                now=now,
                plans_dir=safe_runtime.plans_dir,
            )
        assert list(safe_runtime.plans_dir.glob("*.application.json")) == []


def test_noncanonical_plan_bytes_fail_before_attempt_reservation(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    plan = safe_runtime.make_plan()
    path, digest = write_plan(plan, safe_runtime.plans_dir)
    path.write_text(json.dumps(plan.to_mapping(), indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(DeploymentError) as caught:
        reserve_plan_attempt(
            path,
            confirmed_sha256=digest,
            now=plan.created_at,
            plans_dir=safe_runtime.plans_dir,
        )

    assert caught.value.code == "CMMS-E011"
    assert list(safe_runtime.plans_dir.glob("*.application.json")) == []


def test_reservation_exclusively_consumes_one_valid_plan_hash(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    plan = safe_runtime.make_plan()
    path, digest = write_plan(plan, safe_runtime.plans_dir)
    reservation = reserve_plan_attempt(
        path,
        confirmed_sha256=digest,
        now=plan.created_at,
        plans_dir=safe_runtime.plans_dir,
        application_id="1" * 32,
    )

    assert reservation.application.state is PlanApplicationState.ATTEMPTED
    assert reservation.application.application_generation == 1
    assert reservation.application_path.name == f"{digest}.application.json"
    with pytest.raises(DeploymentError):
        reserve_plan_attempt(
            path,
            confirmed_sha256=digest,
            now=plan.created_at,
            plans_dir=safe_runtime.plans_dir,
            application_id="2" * 32,
        )


def test_plan_application_record_enforces_generation_and_state_graph() -> None:
    attempted = PlanApplicationRecord.attempted(
        plan_sha256="1" * 64,
        now=NOW,
        application_id="a" * 32,
    )
    claimed = attempted.transition(
        PlanApplicationState.IN_PROGRESS,
        now=NOW + timedelta(seconds=1),
    )
    succeeded = claimed.transition(
        PlanApplicationState.SUCCEEDED,
        now=NOW + timedelta(seconds=2),
    )

    assert attempted.application_generation == 1
    assert claimed.application_generation == 2
    assert succeeded.application_generation == 3
    assert succeeded.safe_result_codes == (ApplicationResultCode.APPLY_SUCCEEDED,)
    assert succeeded.application_id == attempted.application_id
    assert succeeded.plan_sha256 == attempted.plan_sha256
    assert succeeded.attempted_at == attempted.attempted_at

    with pytest.raises(DeploymentError) as caught:
        attempted.transition(
            PlanApplicationState.SUCCEEDED,
            now=NOW + timedelta(seconds=1),
        )
    assert caught.value.code == "CMMS-E011"
    with pytest.raises(DeploymentError):
        succeeded.transition(PlanApplicationState.FAILED, now=NOW + timedelta(seconds=3))


def test_application_transition_accepts_exact_secondary_result_codes_keyword() -> None:
    attempted = PlanApplicationRecord.attempted("1" * 64, NOW, "a" * 32)
    claimed = attempted.transition(
        PlanApplicationState.IN_PROGRESS,
        NOW + timedelta(seconds=1),
        secondary_result_codes=(),
    )
    assert claimed.state is PlanApplicationState.IN_PROGRESS


@pytest.mark.parametrize("state", [PlanApplicationState.CONTENDED, PlanApplicationState.REJECTED])
def test_preclaim_terminal_application_states_have_exact_primary_code(
    state: PlanApplicationState,
) -> None:
    attempted = PlanApplicationRecord.attempted("1" * 64, NOW, "a" * 32)
    terminal = attempted.transition(state, NOW + timedelta(seconds=1))

    expected = (
        ApplicationResultCode.APPLY_CONTENDED
        if state is PlanApplicationState.CONTENDED
        else ApplicationResultCode.APPLY_REJECTED
    )
    assert terminal.claimed_at is None
    assert terminal.terminal_at == NOW + timedelta(seconds=1)
    assert terminal.safe_result_codes == (expected,)


def test_plan_application_rejects_invalid_id_time_codes_and_combinations() -> None:
    with pytest.raises(DeploymentError):
        PlanApplicationRecord.attempted("1" * 64, NOW, "A" * 32)
    attempted = PlanApplicationRecord.attempted("1" * 64, NOW, "a" * 32)
    with pytest.raises(DeploymentError):
        attempted.transition(PlanApplicationState.IN_PROGRESS, NOW)
    with pytest.raises(DeploymentError):
        attempted.transition(
            PlanApplicationState.CONTENDED,
            NOW + timedelta(seconds=1),
            (ApplicationResultCode.APPLY_FAILED,),
        )

    mapping = attempted.to_mapping()
    invalid = []
    for key in mapping:
        candidate = dict(mapping)
        del candidate[key]
        invalid.append(candidate)
    unknown = dict(mapping)
    unknown["unknown"] = None
    invalid.append(unknown)
    bad_state = dict(mapping)
    bad_state["state"] = "UNKNOWN"
    invalid.append(bad_state)
    bad_generation = dict(mapping)
    bad_generation["application_generation"] = 2
    invalid.append(bad_generation)
    for candidate in invalid:
        with pytest.raises(DeploymentError):
            PlanApplicationRecord.from_mapping(candidate)


def test_plan_application_loader_requires_filename_plan_hash_match(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    record = PlanApplicationRecord.attempted("1" * 64, NOW, "a" * 32)
    path = safe_runtime.private_file(
        ".runtime/plans/cmms-development/" + "2" * 64 + ".application.json",
        canonical_json_bytes(record.to_mapping()),
    )
    with pytest.raises(DeploymentError):
        PlanApplicationRecord.load(path)


def _confirmed_plan(
    safe_runtime: SafeRuntimeFixture,
    *,
    nonce: str = "a" * 32,
    application_id: str = "b" * 32,
) -> tuple[DeploymentPlan, object, object, ConfirmedDeploymentPlan]:
    plan = safe_runtime.make_plan(plan_nonce=nonce)
    path, digest = write_plan(plan, safe_runtime.plans_dir)
    reservation = reserve_plan_attempt(
        path,
        confirmed_sha256=digest,
        now=plan.created_at,
        plans_dir=safe_runtime.plans_dir,
        application_id=application_id,
    )
    lease = safe_runtime.acquire_deployment_write_lease(reservation)
    confirmed = load_confirmed_plan(
        reservation,
        snapshot=plan.snapshot,
        current_bootstrap_bindings=plan.bootstrap_bindings,
        lease=lease,
    )
    return plan, reservation, lease, confirmed


def test_confirmed_plan_rejects_changed_source_before_effect(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    plan = safe_runtime.make_plan(operation=Operation.START)
    plan_path, plan_hash = write_plan(plan, safe_runtime.plans_dir)
    changed_source = replace(plan.snapshot.source, cmms_head="1" * 40)
    changed = replace(plan.snapshot, source=changed_source)

    with pytest.raises(DeploymentError) as caught:
        reservation = reserve_plan_attempt(
            plan_path,
            confirmed_sha256=plan_hash,
            now=plan.created_at + timedelta(minutes=1),
            plans_dir=safe_runtime.plans_dir,
        )
        lease = safe_runtime.acquire_deployment_write_lease(reservation)
        load_confirmed_plan(
            reservation,
            snapshot=changed,
            current_bootstrap_bindings=safe_runtime.bootstrap_plan_bindings,
            lease=lease,
        )

    assert caught.value.code == "CMMS-E012"
    assert safe_runtime.runner.calls == []


@pytest.mark.parametrize("field", ["config_sha256", "unit_generation", "state_sha256"])
def test_confirmation_rejects_changed_snapshot_binding_and_consumes_hash(
    safe_runtime: SafeRuntimeFixture,
    field: str,
) -> None:
    plan = safe_runtime.make_plan()
    path, digest = write_plan(plan, safe_runtime.plans_dir)
    reservation = reserve_plan_attempt(
        path,
        confirmed_sha256=digest,
        now=plan.created_at,
        plans_dir=safe_runtime.plans_dir,
    )
    lease = safe_runtime.acquire_deployment_write_lease(reservation)
    changed = replace(plan.snapshot, **{field: "8" * 64})

    with pytest.raises(DeploymentError) as caught:
        load_confirmed_plan(
            reservation,
            snapshot=changed,
            current_bootstrap_bindings=plan.bootstrap_bindings,
            lease=lease,
        )

    assert caught.value.code == "CMMS-E012"
    assert PlanApplicationRecord.load(reservation.application_path).state is (
        PlanApplicationState.REJECTED
    )


def test_confirmation_rejects_changed_bootstrap_file_stat(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    plan = safe_runtime.make_plan()
    path, digest = write_plan(plan, safe_runtime.plans_dir)
    reservation = reserve_plan_attempt(
        path,
        confirmed_sha256=digest,
        now=plan.created_at,
        plans_dir=safe_runtime.plans_dir,
    )
    lease = safe_runtime.acquire_deployment_write_lease(reservation)
    credentials = list(plan.bootstrap_bindings.credentials or ())
    credentials[1] = replace(
        credentials[1],
        current_file=replace(credentials[1].current_file, ino=9999),
    )
    changed = replace(plan.bootstrap_bindings, credentials=tuple(credentials))

    with pytest.raises(DeploymentError):
        load_confirmed_plan(reservation, plan.snapshot, changed, lease)


def test_two_plans_cannot_hold_write_lease_and_loser_only_contends(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    first = safe_runtime.make_plan(plan_nonce="1" * 32)
    second = safe_runtime.make_plan(plan_nonce="2" * 32)
    first_path, first_hash = write_plan(first, safe_runtime.plans_dir)
    second_path, second_hash = write_plan(second, safe_runtime.plans_dir)
    first_reservation = reserve_plan_attempt(
        first_path,
        first_hash,
        first.created_at,
        safe_runtime.plans_dir,
        application_id="3" * 32,
    )
    second_reservation = reserve_plan_attempt(
        second_path,
        second_hash,
        second.created_at,
        safe_runtime.plans_dir,
        application_id="4" * 32,
    )
    owner = acquire_deployment_write_lease(safe_runtime.lock_path, first_reservation)

    with pytest.raises(DeploymentError) as caught:
        acquire_deployment_write_lease(safe_runtime.lock_path, second_reservation)

    assert caught.value.code == "CMMS-E014"
    assert PlanApplicationRecord.load(first_reservation.application_path).state is (
        PlanApplicationState.ATTEMPTED
    )
    assert PlanApplicationRecord.load(second_reservation.application_path).state is (
        PlanApplicationState.CONTENDED
    )
    assert safe_runtime.runner.calls == []
    owner.close()


def test_inherited_fork_cannot_use_parent_deployment_lease(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    plan = safe_runtime.make_plan(plan_nonce="5" * 32)
    path, digest = write_plan(plan, safe_runtime.plans_dir)
    reservation = reserve_plan_attempt(
        path,
        digest,
        plan.created_at,
        safe_runtime.plans_dir,
        application_id="6" * 32,
    )
    lease = acquire_deployment_write_lease(safe_runtime.lock_path, reservation)
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid == 0:
        os.close(read_fd)
        try:
            load_confirmed_plan(
                reservation,
                plan.snapshot,
                plan.bootstrap_bindings,
                lease,
            )
        except DeploymentError as error:
            os.write(write_fd, error.code.encode("ascii"))
        else:
            os.write(write_fd, b"LEASE_ACCEPTED")
        finally:
            os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    try:
        result = os.read(read_fd, 64).decode("ascii")
        waited_pid, status = os.waitpid(child_pid, 0)
        assert waited_pid == child_pid
        assert os.waitstatus_to_exitcode(status) == 0
        assert result == "CMMS-E012"
        load_confirmed_plan(
            reservation,
            plan.snapshot,
            plan.bootstrap_bindings,
            lease,
        )
    finally:
        os.close(read_fd)
        lease.close()


def test_lock_path_inode_replacement_cannot_create_a_second_valid_lease(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    first = safe_runtime.make_plan(plan_nonce="7" * 32)
    second = safe_runtime.make_plan(plan_nonce="8" * 32)
    first_path, first_hash = write_plan(first, safe_runtime.plans_dir)
    second_path, second_hash = write_plan(second, safe_runtime.plans_dir)
    first_reservation = reserve_plan_attempt(
        first_path,
        first_hash,
        first.created_at,
        safe_runtime.plans_dir,
        application_id="9" * 32,
    )
    second_reservation = reserve_plan_attempt(
        second_path,
        second_hash,
        second.created_at,
        safe_runtime.plans_dir,
        application_id="a" * 32,
    )
    owner = acquire_deployment_write_lease(safe_runtime.lock_path, first_reservation)
    displaced = safe_runtime.runtime_dir / "displaced-apply.lock"
    os.replace(safe_runtime.lock_path, displaced)
    safe_runtime.lock_path.write_bytes(b"")
    safe_runtime.lock_path.chmod(0o600)
    try:
        with pytest.raises(DeploymentError) as caught:
            acquire_deployment_write_lease(
                safe_runtime.lock_path,
                second_reservation,
            )
        assert caught.value.code == "CMMS-E014"
        assert PlanApplicationRecord.load(second_reservation.application_path).state is (
            PlanApplicationState.CONTENDED
        )
        with pytest.raises(DeploymentError) as stale:
            load_confirmed_plan(
                first_reservation,
                first.snapshot,
                first.bootstrap_bindings,
                owner,
            )
        assert stale.value.code == "CMMS-E012"
    finally:
        owner.close()


def test_deployment_lease_rejects_noncanonical_lock_path_spelling(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    plan = safe_runtime.make_plan(plan_nonce="b" * 32)
    path, digest = write_plan(plan, safe_runtime.plans_dir)
    reservation = reserve_plan_attempt(
        path,
        digest,
        plan.created_at,
        safe_runtime.plans_dir,
        application_id="c" * 32,
    )
    alias = (
        safe_runtime.runtime_dir
        / "plans"
        / ".."
        / "cmms-development-apply.lock"
    )
    with pytest.raises(DeploymentError) as caught:
        acquire_deployment_write_lease(alias, reservation)
    assert caught.value.code == "CMMS-E012"
    assert PlanApplicationRecord.load(reservation.application_path).state is (
        PlanApplicationState.ATTEMPTED
    )


@pytest.mark.parametrize(
    "capability",
    [
        ConfirmedDeploymentPlan,
        PlanAttemptReservation,
        DeploymentWriteLease,
        FailClosedEvidence,
        ClaimedApplyContext,
        GatewayListenerChallenge,
        GatewayListenerAbsentProof,
    ],
)
def test_opaque_capability_public_construction_fails(capability: type[object]) -> None:
    with pytest.raises((DeploymentError, TypeError)):
        capability()


def test_confirmed_plan_exposes_only_immutable_plan_payload(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    plan, _reservation, lease, confirmed = _confirmed_plan(safe_runtime)
    assert confirmed.plan is plan
    with pytest.raises((AttributeError, FrozenInstanceError)):
        confirmed.plan = plan  # type: ignore[misc]
    lease.close()


def test_gateway_authority_exact_graph_and_replay_rejection(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    _plan, _reservation, lease, confirmed = _confirmed_plan(safe_runtime)
    adapter = safe_runtime.gateway_authority(confirmed, lease)
    challenge = adapter.challenge()
    proof = adapter.prove(challenge)
    evidence = adapter.parts.evidence_issuer.issue(confirmed, lease, proof)

    with pytest.raises(DeploymentError):
        adapter.prove(challenge)
    with pytest.raises(DeploymentError):
        adapter.parts.evidence_issuer.issue(confirmed, lease, proof)
    with pytest.raises(Exception):
        copy.copy(challenge)
    with pytest.raises(Exception):
        pickle.dumps(proof)

    context = claim_plan_application(
        confirmed,
        safe_runtime.plans_dir,
        lease,
        evidence,
    )
    assert context.application.state is PlanApplicationState.IN_PROGRESS
    with pytest.raises(DeploymentError):
        claim_plan_application(
            confirmed,
            safe_runtime.plans_dir,
            lease,
            evidence,
        )
    lease.close()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("loaded_generation", "0" * 64),
        ("loaded_sha256", "0" * 64),
        ("checked_ipv4", "172.18.0.1"),
        ("checked_port", 3001),
        ("listener_count", 1),
    ],
)
def test_gateway_proof_rejects_every_listener_binding_mismatch(
    safe_runtime: SafeRuntimeFixture,
    field: str,
    value: object,
) -> None:
    _plan, _reservation, lease, confirmed = _confirmed_plan(safe_runtime)
    adapter = safe_runtime.gateway_authority(confirmed, lease)
    challenge = adapter.challenge()
    kwargs = {
        "loaded_generation": adapter.gateway_generation,
        "loaded_sha256": adapter.loopback_gateway_sha256,
        "checked_ipv4": adapter.gateway_ipv4,
        "checked_port": 3000,
        "listener_count": 0,
    }
    kwargs[field] = value
    with pytest.raises(DeploymentError):
        adapter.parts.listener_proof_mint.mint_absent(challenge, **kwargs)
    lease.close()


def test_gateway_authority_rejects_cross_authority_proof(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    _plan, _reservation, lease, confirmed = _confirmed_plan(safe_runtime)
    first = safe_runtime.gateway_authority(confirmed, lease)
    second = safe_runtime.gateway_authority(confirmed, lease)
    challenge = first.challenge()
    with pytest.raises(DeploymentError):
        second.prove(challenge)
    lease.close()


def test_claim_rejects_evidence_for_another_plan(
    safe_runtime: SafeRuntimeFixture,
) -> None:
    _first_plan, _first_reservation, first_lease, first = _confirmed_plan(
        safe_runtime,
        nonce="1" * 32,
        application_id="2" * 32,
    )
    evidence = safe_runtime.gateway_authority(first, first_lease).issue()
    first_lease.close()

    _second_plan, _second_reservation, second_lease, second = _confirmed_plan(
        safe_runtime,
        nonce="3" * 32,
        application_id="4" * 32,
    )
    with pytest.raises(DeploymentError):
        claim_plan_application(
            second,
            safe_runtime.plans_dir,
            second_lease,
            evidence,
        )
    second_lease.close()


def _state_mapping() -> dict[str, object]:
    return {
        "schema_version": 1,
        "record_type": "cmms-development-state",
        "generation": 1,
        "root_sha": "1" * 40,
        "root_dirty_fingerprint": "2" * 64,
        "root_status": "CLEAN",
        "cmms_gitlink": "3" * 40,
        "cmms_head": "3" * 40,
        "cmms_dirty_fingerprint": "4" * 64,
        "cmms_status": "CLEAN",
        "config_sha256": "5" * 64,
        "toolchain_manifest_sha256": "6" * 64,
        "sensitive_manifest_sha256": "7" * 64,
        "api_artifact_sha256": "8" * 64,
        "frontend_lock_sha256": "9" * 64,
        "controller_entrypoint_sha256": "a" * 64,
        "controller_package_sha256": "b" * 64,
        "unit_generation": "c" * 64,
        "compose_project": "ifactory-cmms-dev",
        "postgres_volume_name": "ifactory-cmms-dev_postgres_data",
        "postgres_volume_identity": "d" * 64,
        "minio_volume_name": "ifactory-cmms-dev_minio_data",
        "minio_volume_identity": "e" * 64,
        "api_main_pid": None,
        "api_process_start_ticks": None,
        "frontend_main_pid": None,
        "frontend_process_start_ticks": None,
        "docker_gateway_ipv4": None,
        "loopback_gateway_sha256": None,
        "gateway_generation": None,
        "gateway_mode": "STOPPED",
        "license_mode": "offline",
        "license_guard_generation": None,
        "online_budget_ledger_sha256": None,
        "latest_budget_debit_id": None,
        "latest_budget_sequence": None,
        "latest_budget_local_date": None,
        "bootstrap_receipt_sha256": None,
        "bootstrap_state": "UNINITIALIZED",
        "last_plan_sha256": "f" * 64,
        "last_operation": "stop",
        "last_transition_code": "STATE_INITIALIZED",
        "updated_at": "2026-07-31T12:00:00.000000Z",
    }


def test_state_record_round_trips_complete_fixed_schema() -> None:
    value = _state_mapping()
    record = StateRecord.from_mapping(value)
    assert record.to_mapping() == value
    assert StateRecord.from_bytes(canonical_json_bytes(value)) == record


def test_state_public_constructor_validates_and_exposes_complete_typed_surface() -> None:
    invalid = _state_mapping()
    invalid["generation"] = 0
    with pytest.raises(DeploymentError):
        StateRecord(invalid)

    record = StateRecord(_state_mapping())
    for field, expected in _state_mapping().items():
        actual = getattr(record, field)
        if field in {"root_status", "cmms_status"}:
            assert isinstance(actual, SourceStatus)
        elif field == "gateway_mode":
            assert isinstance(actual, GatewayMode)
        elif field == "license_mode":
            assert isinstance(actual, LicenseMode)
        elif field == "bootstrap_state":
            assert isinstance(actual, BootstrapState)
        elif field == "last_operation":
            assert isinstance(actual, Operation)
        elif field == "updated_at":
            assert isinstance(actual, datetime)
        else:
            assert actual == expected


def test_state_record_rejects_every_missing_field_and_unknown_field() -> None:
    value = _state_mapping()
    for field in value:
        candidate = dict(value)
        del candidate[field]
        with pytest.raises(DeploymentError):
            StateRecord.from_mapping(candidate)
    candidate = dict(value)
    candidate["unknown"] = None
    with pytest.raises(DeploymentError):
        StateRecord.from_mapping(candidate)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("generation", 0),
        ("root_sha", "A" * 40),
        ("root_status", "UNKNOWN"),
        ("unit_generation", "short"),
        ("compose_project", "other"),
        ("postgres_volume_name", "other"),
        ("docker_gateway_ipv4", "0.0.0.0"),
        ("latest_budget_local_date", "2026-02-30"),
        ("last_transition_code", "not lowercase"),
        ("updated_at", "2026-07-31T12:00:00Z"),
    ],
)
def test_state_record_rejects_invalid_scalar_grammar(field: str, value: object) -> None:
    mapping = _state_mapping()
    mapping[field] = value
    with pytest.raises(DeploymentError):
        StateRecord.from_mapping(mapping)


def test_state_gateway_process_guard_budget_and_bootstrap_matrices() -> None:
    loopback = _state_mapping()
    loopback.update(
        docker_gateway_ipv4="172.17.0.1",
        loopback_gateway_sha256="1" * 64,
        gateway_generation="2" * 64,
        gateway_mode="LOOPBACK",
    )
    StateRecord.from_mapping(loopback)

    dual = dict(loopback)
    dual["gateway_mode"] = "DUAL"
    with pytest.raises(DeploymentError):
        StateRecord.from_mapping(dual)
    dual.update(
        api_main_pid=101,
        api_process_start_ticks=201,
        frontend_main_pid=102,
        frontend_process_start_ticks=202,
        license_guard_generation="3" * 64,
    )
    StateRecord.from_mapping(dual)

    broken_guard = dict(dual)
    broken_guard["license_guard_generation"] = None
    with pytest.raises(DeploymentError):
        StateRecord.from_mapping(broken_guard)

    online = dict(dual)
    online["license_mode"] = "online"
    online["license_guard_generation"] = None
    online.update(
        online_budget_ledger_sha256="4" * 64,
        latest_budget_debit_id="5" * 32,
        latest_budget_sequence=1,
        latest_budget_local_date="2026-07-31",
    )
    StateRecord.from_mapping(online)

    broken_budget = dict(online)
    broken_budget["latest_budget_sequence"] = None
    with pytest.raises(DeploymentError):
        StateRecord.from_mapping(broken_budget)

    broken_bootstrap = dict(loopback)
    broken_bootstrap["bootstrap_state"] = "ADMIN_ROTATED"
    with pytest.raises(DeploymentError):
        StateRecord.from_mapping(broken_bootstrap)


def test_state_successor_requires_first_generation_exact_increment_and_time() -> None:
    first = StateRecord.from_mapping(_state_mapping())
    first.require_successor(None)

    second_mapping = _state_mapping()
    second_mapping["generation"] = 2
    second_mapping["updated_at"] = "2026-07-31T12:00:01.000000Z"
    second = StateRecord.from_mapping(second_mapping)
    second.require_successor(first)

    skipped = dict(second_mapping)
    skipped["generation"] = 3
    with pytest.raises(DeploymentError):
        StateRecord.from_mapping(skipped).require_successor(first)
    stale = dict(second_mapping)
    stale["updated_at"] = _state_mapping()["updated_at"]
    with pytest.raises(DeploymentError):
        StateRecord.from_mapping(stale).require_successor(first)


def test_state_successor_cannot_clear_established_budget_anchor() -> None:
    previous_mapping = _state_mapping()
    previous_mapping.update(
        online_budget_ledger_sha256="1" * 64,
        latest_budget_debit_id="2" * 32,
        latest_budget_sequence=1,
        latest_budget_local_date="2026-07-31",
    )
    previous = StateRecord.from_mapping(previous_mapping)
    next_mapping = _state_mapping()
    next_mapping["generation"] = 2
    next_mapping["updated_at"] = "2026-07-31T12:00:01.000000Z"
    current = StateRecord.from_mapping(next_mapping)
    with pytest.raises(DeploymentError):
        current.require_successor(previous)


SCHEMAS = (
    START_PERMIT_SCHEMA,
    BUDGET_LEDGER_SCHEMA,
    BUDGET_RECOVERY_RECEIPT_SCHEMA,
    BOOTSTRAP_RECEIPT_SCHEMA,
    ACCEPTANCE_RECEIPT_SCHEMA,
)

FROZEN_SCHEMA_EXPECTATIONS = {
    "StartPermit": (
        ("schema_version", "nonce", "plan_sha256", "created_at", "expires_at", "uid", "root_sha", "cmms_source_fingerprint", "api_artifact_sha256", "controller_entrypoint_sha256", "controller_package_sha256", "unit_generation", "loopback_gateway_sha256", "docker_gateway_ipv4", "license_mode", "budget_debit_id"),
        {"schema_version": 1},
    ),
    "BudgetLedger": (
        ("schema_version", "zone", "local_date", "controlled_limit", "source_limit", "attempts", "previous_ledger_sha256", "continuity_state"),
        {"schema_version": 1, "zone": "Asia/Shanghai", "controlled_limit": 10, "source_limit": 20},
    ),
    "BudgetRecoveryReceipt": (
        ("schema_version", "record_type", "status", "plan_sha256", "local_date", "unknown_reason_code", "original_bytes_present", "original_ledger_sha256", "recovery_debit_id", "api_main_pid", "api_process_start_ticks", "safe_result_code", "created_at", "updated_at"),
        {"schema_version": 1, "record_type": "cmms-budget-recovery-receipt"},
    ),
    "BootstrapReceipt": (
        ("schema_version", "record_type", "receipt_generation", "origin_plan_sha256", "last_plan_sha256", "state", "super_admin_user_id", "company_id", "company_settings_id", "organization_admin_user_id", "role_id", "invitation_email_hash", "runtime_user_id", "api_key_id", "api_key_label", "api_key_capture_attempt_id", "api_key_capture_status", "api_key_capture_file", "phase2_publish_status", "phase2_env_file", "api_key_capture_cleared", "revoked_api_key_ids", "action_attempts", "probe_user_id", "action_result_codes", "created_at", "updated_at"),
        {"schema_version": 1, "record_type": "cmms-bootstrap-receipt"},
    ),
    "AcceptanceReceipt": (
        ("schema_version", "record_type", "plan_sha256", "root_sha", "cmms_gitlink", "cmms_head", "api_artifact_sha256", "controller_entrypoint_sha256", "controller_package_sha256", "unit_generation", "api_main_pid", "api_process_start_ticks", "gateway_ipv4", "gateway_generation", "license_mode", "company_id", "organization_admin_user_id", "runtime_user_id", "role_id", "api_key_id", "asset_total", "work_order_total", "preopen_report_sha256", "preopen_check_codes", "postopen_report_sha256", "postopen_check_codes", "minio_probe_result_sha256", "minio_probe_check_codes", "minio_cleanup_code", "checked_at"),
        {"schema_version": 1, "record_type": "cmms-acceptance-receipt"},
    ),
}


def test_fixed_schema_envelopes_match_frozen_literal_contract() -> None:
    assert {
        schema.record_name: (schema.required_fields, dict(schema.fixed_values))
        for schema in SCHEMAS
    } == FROZEN_SCHEMA_EXPECTATIONS


def _schema_value(schema: FixedRecordSchema) -> dict[str, object]:
    value = {field: None for field in schema.required_fields}
    value.update(schema.fixed_values)
    return value


@pytest.mark.parametrize("schema", SCHEMAS, ids=lambda item: item.record_name)
def test_fixed_record_schema_rejects_every_missing_and_unknown_field(
    schema: FixedRecordSchema,
) -> None:
    value = _schema_value(schema)
    require_exact_record_fields(value, schema)
    for field in schema.required_fields:
        candidate = dict(value)
        del candidate[field]
        with pytest.raises(DeploymentError):
            require_exact_record_fields(candidate, schema)
    candidate = dict(value)
    candidate["unknown"] = None
    with pytest.raises(DeploymentError):
        require_exact_record_fields(candidate, schema)


@pytest.mark.parametrize("schema", SCHEMAS, ids=lambda item: item.record_name)
def test_fixed_record_schema_rejects_each_changed_fixed_literal(
    schema: FixedRecordSchema,
) -> None:
    value = _schema_value(schema)
    for field in schema.fixed_values:
        candidate = dict(value)
        candidate[field] = "changed"
        with pytest.raises(DeploymentError):
            require_exact_record_fields(candidate, schema)


def test_receipt_structural_schemas_reject_each_other_discriminators() -> None:
    receipt_schemas = (
        BUDGET_RECOVERY_RECEIPT_SCHEMA,
        BOOTSTRAP_RECEIPT_SCHEMA,
        ACCEPTANCE_RECEIPT_SCHEMA,
    )
    for schema in receipt_schemas:
        value = _schema_value(schema)
        for other in receipt_schemas:
            if other is schema:
                continue
            candidate = dict(value)
            candidate["record_type"] = other.fixed_values["record_type"]
            with pytest.raises(DeploymentError):
                require_exact_record_fields(candidate, schema)


@pytest.mark.parametrize("schema", SCHEMAS, ids=lambda item: item.record_name)
def test_fixed_schemas_reject_secret_marker_recursively(schema: FixedRecordSchema) -> None:
    value = _schema_value(schema)
    mutable = next(field for field in schema.required_fields if field not in schema.fixed_values)
    value[mutable] = {"safe": SENTINEL_SECRET}
    with pytest.raises(DeploymentError):
        require_exact_record_fields(value, schema)
