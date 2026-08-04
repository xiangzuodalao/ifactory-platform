#!/usr/bin/env python3
"""Fail-closed validation for the closed-loop pilot host runtime."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
from uuid import UUID


ENV_NAME = "closed-loop-pilot.env"
GENERATED_ENV_NAME = "closed-loop-generated.env"
STATE_LOCK_NAME = ".state.lock"
GENERATED_ENV_KEYS = (
    "PLATFORM_INTEGRATION_TB_TENANT_ID",
    "PLATFORM_INTEGRATION_CMMS_COMPANY_ID",
    "PLATFORM_INTEGRATION_APPROVER_TB_USER_ID",
    "PLATFORM_INTEGRATION_PILOT_WORK_ORDER_EQUIPMENT_ID",
    "PILOT_CMMS_CREDENTIAL",
)
MAX_ENV_BYTES = 256 * 1024
MAX_RECEIPT_BYTES = 64 * 1024
SECRET_FILES = {
    "CMMS_POSTGRES_PASSWORD_FILE": "cmms-postgres-password",
    "CMMS_JWT_SECRET_FILE": "cmms-jwt-secret",
    "CMMS_ADMIN_PASSWORD_FILE": "cmms-admin-password",
    "MINIO_ROOT_USER_FILE": "minio-root-user",
    "MINIO_ROOT_PASSWORD_FILE": "minio-root-password",
}
KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")


class PreflightError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _metadata(path: Path, *, kind: str, mode: int) -> os.stat_result:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        raise PreflightError(f"{kind}_INVALID") from None
    expected_type = stat.S_ISDIR if kind.endswith("DIR") else stat.S_ISREG
    if (
        not expected_type(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != mode
        or metadata.st_uid != os.getuid()
        or metadata.st_gid != os.getgid()
        or path.is_symlink()
        or (not kind.endswith("DIR") and metadata.st_nlink != 1)
    ):
        raise PreflightError(f"{kind}_INVALID")
    return metadata


def _read_regular(path: Path, *, kind: str, mode: int, maximum: int) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise PreflightError(f"{kind}_INVALID") from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != mode
            or metadata.st_uid != os.getuid()
            or metadata.st_gid != os.getgid()
            or metadata.st_nlink != 1
        ):
            raise PreflightError(f"{kind}_INVALID")
        chunks: list[bytes] = []
        length = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - length))
            if not chunk:
                break
            chunks.append(chunk)
            length += len(chunk)
            if length > maximum:
                raise PreflightError(f"{kind}_INVALID")
        return b"".join(chunks)
    except OSError:
        raise PreflightError(f"{kind}_INVALID") from None
    finally:
        os.close(descriptor)


def parse_env_bytes(raw: bytes, *, kind: str = "PILOT_ENV_FILE") -> dict[str, str]:
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeError:
        raise PreflightError(f"{kind}_INVALID") from None
    values: dict[str, str] = {}
    for line in lines:
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise PreflightError(f"{kind}_INVALID")
        key, value = line.split("=", 1)
        if KEY.fullmatch(key) is None or key in values or any(ord(char) < 32 for char in value):
            raise PreflightError(f"{kind}_INVALID")
        values[key] = value
    return values


def parse_env(path: Path, *, kind: str = "PILOT_ENV_FILE") -> dict[str, str]:
    raw = _read_regular(path, kind=kind, mode=0o600, maximum=MAX_ENV_BYTES)
    return parse_env_bytes(raw, kind=kind)


def _credential(values: dict[str, str], key: str, kind: str) -> str:
    try:
        envelope = json.loads(values.get(key, ""))
    except (json.JSONDecodeError, TypeError):
        raise PreflightError(f"{key}_INVALID") from None
    if (
        type(envelope) is not dict
        or set(envelope) != {"kind", "value"}
        or envelope.get("kind") != kind
        or type(envelope.get("value")) is not str
        or not envelope["value"]
    ):
        raise PreflightError(f"{key}_INVALID")
    return envelope["value"]


def _uuid(value: str, code: str) -> str:
    try:
        canonical = str(UUID(value))
    except (ValueError, AttributeError):
        raise PreflightError(code) from None
    if canonical != value:
        raise PreflightError(code)
    return value


def _receipt(runtime: Path, name: str, schema: str) -> dict[str, object]:
    receipts = runtime / "receipts"
    _metadata(receipts, kind="PILOT_RECEIPTS_DIR", mode=0o700)
    path = receipts / name
    try:
        raw = _read_regular(
            path,
            kind="PILOT_RECEIPT_FILE",
            mode=0o600,
            maximum=MAX_RECEIPT_BYTES,
        )
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        raise PreflightError("PILOT_RECEIPT_INVALID") from None
    if (
        type(payload) is not dict
        or payload.get("schema_version") != schema
        or payload.get("status") != "SUCCEEDED"
        or re.fullmatch(r"[0-9a-f]{64}", str(payload.get("plan_sha256", ""))) is None
    ):
        raise PreflightError("PILOT_RECEIPT_INVALID")
    return payload


def _validate_stage(
    state: Path,
    runtime: Path,
    values: dict[str, str],
    stage: str,
) -> None:
    if stage == "base":
        return
    _credential(values, "PILOT_TB_CREDENTIAL", "thingsboard_bearer")
    if stage == "bootstrap":
        return

    tenant_id = _uuid(values.get("PLATFORM_INTEGRATION_TENANT_ID", ""), "PILOT_TENANT_ID_INVALID")
    tb_tenant_id = _uuid(
        values.get("PLATFORM_INTEGRATION_TB_TENANT_ID", ""), "TB_TENANT_ID_INVALID"
    )
    approver = _uuid(
        values.get("PLATFORM_INTEGRATION_APPROVER_TB_USER_ID", ""),
        "TB_APPROVER_USER_ID_INVALID",
    )
    try:
        company_id = int(values.get("PLATFORM_INTEGRATION_CMMS_COMPANY_ID", ""))
    except ValueError:
        raise PreflightError("CMMS_COMPANY_ID_INVALID") from None
    if company_id <= 0 or str(company_id) != values.get("PLATFORM_INTEGRATION_CMMS_COMPANY_ID"):
        raise PreflightError("CMMS_COMPANY_ID_INVALID")
    _credential(values, "PILOT_CMMS_CREDENTIAL", "cmms_bearer")
    bootstrap = _receipt(state, "closed-loop-bootstrap.json", "closed-loop-bootstrap-v1")
    if (
        bootstrap.get("tenant_id") != tenant_id
        or bootstrap.get("tb_tenant_id") != tb_tenant_id
        or bootstrap.get("tb_approver_user_id") != approver
        or bootstrap.get("cmms_company_id") != company_id
    ):
        raise PreflightError("BOOTSTRAP_RECEIPT_BINDING_INVALID")
    if stage == "provision":
        return

    equipment_id = _uuid(
        values.get("PLATFORM_INTEGRATION_PILOT_WORK_ORDER_EQUIPMENT_ID", ""),
        "PILOT_EQUIPMENT_ID_INVALID",
    )
    provision = _receipt(state, "closed-loop-provision.json", "closed-loop-provision-v1")
    if (
        provision.get("tenant_id") != tenant_id
        or provision.get("tb_tenant_id") != tb_tenant_id
        or provision.get("cmms_company_id") != company_id
        or provision.get("equipment_id") != equipment_id
    ):
        raise PreflightError("PROVISION_RECEIPT_BINDING_INVALID")
    pdm_token = _credential(values, "PILOT_PDM_CREDENTIAL", "opaque_bearer")
    if pdm_token != values.get("VALEO_PDM_PREDICTION_V2_BEARER_TOKEN"):
        raise PreflightError("PDM_CREDENTIAL_BINDING_INVALID")
    fixtures = runtime / "pdm-fixtures"
    manifest = fixtures / "manifest.runtime.yaml"
    objects = fixtures / "objects"
    try:
        fixture_meta = fixtures.stat(follow_symlinks=False)
        manifest_meta = manifest.stat(follow_symlinks=False)
        objects_meta = objects.stat(follow_symlinks=False)
    except OSError:
        raise PreflightError("PDM_FIXTURES_INVALID") from None
    if (
        not stat.S_ISDIR(fixture_meta.st_mode)
        or not stat.S_ISREG(manifest_meta.st_mode)
        or not stat.S_ISDIR(objects_meta.st_mode)
        or fixtures.is_symlink()
        or manifest.is_symlink()
        or objects.is_symlink()
        or manifest_meta.st_nlink != 1
        or any(
            meta.st_uid != os.getuid() or meta.st_gid != os.getgid()
            for meta in (fixture_meta, manifest_meta, objects_meta)
        )
    ):
        raise PreflightError("PDM_FIXTURES_INVALID")


def validate(repo_root: Path, env_file: Path, stage: str = "base") -> dict[str, str]:
    expected_repo = Path(__file__).resolve().parents[3]
    try:
        canonical_repo = repo_root.resolve(strict=True)
    except OSError:
        raise PreflightError("PILOT_REPOSITORY_INVALID") from None
    if canonical_repo != expected_repo or repo_root.is_symlink():
        raise PreflightError("PILOT_REPOSITORY_INVALID")

    runtime = expected_repo / ".runtime"
    _metadata(runtime, kind="PILOT_RUNTIME_DIR", mode=0o700)
    control = runtime / "closed-loop"
    _metadata(control, kind="PILOT_CONTROL_DIR", mode=0o700)
    state = control / "state"
    _metadata(state, kind="PILOT_STATE_DIR", mode=0o700)
    _metadata(state / "receipts", kind="PILOT_RECEIPTS_DIR", mode=0o700)
    expected_env = control / ENV_NAME
    if env_file != expected_env:
        raise PreflightError("PILOT_ENV_FILE_INVALID")
    base_values = parse_env(env_file)
    generated_env = state / GENERATED_ENV_NAME
    generated_values = parse_env(generated_env, kind="PILOT_GENERATED_ENV_FILE")
    if set(generated_values) != set(GENERATED_ENV_KEYS):
        raise PreflightError("PILOT_GENERATED_ENV_FILE_INVALID")
    if set(base_values) & set(GENERATED_ENV_KEYS):
        raise PreflightError("PILOT_ENV_FILE_INVALID")
    values = {**base_values, **generated_values}
    if values.get("PILOT_RUNTIME_DIR") != str(runtime):
        raise PreflightError("PILOT_RUNTIME_BINDING_INVALID")
    if values.get("PILOT_STATE_DIR") != str(state):
        raise PreflightError("PILOT_STATE_BINDING_INVALID")
    state_lock = control / STATE_LOCK_NAME
    if values.get("PILOT_STATE_LOCK_FILE") != str(state_lock):
        raise PreflightError("PILOT_STATE_LOCK_BINDING_INVALID")
    _metadata(state_lock, kind="PILOT_STATE_LOCK_FILE", mode=0o600)
    if values.get("PILOT_HOST_UID") != str(os.getuid()):
        raise PreflightError("PILOT_HOST_IDENTITY_INVALID")
    if values.get("PILOT_HOST_GID") != str(os.getgid()):
        raise PreflightError("PILOT_HOST_IDENTITY_INVALID")

    secrets = runtime / "secrets"
    _metadata(secrets, kind="PILOT_SECRETS_DIR", mode=0o700)
    for key, filename in SECRET_FILES.items():
        expected = secrets / filename
        if values.get(key) != str(expected):
            raise PreflightError("PILOT_SECRET_BINDING_INVALID")
        _metadata(expected, kind="PILOT_SECRET_FILE", mode=0o600)
    _validate_stage(state, runtime, values, stage)
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument(
        "--stage", choices=("base", "bootstrap", "provision", "continuous"), default="base"
    )
    args = parser.parse_args()
    try:
        validate(args.repo_root, args.env_file, args.stage)
        return 0
    except PreflightError as exc:
        print(exc.code, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
