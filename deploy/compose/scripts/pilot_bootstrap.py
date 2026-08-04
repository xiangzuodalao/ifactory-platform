#!/usr/bin/env python3
"""Confirmed, secret-safe bootstrap for the isolated closed-loop CMMS identity."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import urllib.error
import urllib.request
from uuid import UUID

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from pilot_state_io import (  # noqa: E402
    StateIoError,
    atomic_replace,
    exclusive_state_lock,
    fsync_directory,
)


COMPANY_NAME = "iFactory Closed Loop Pilot"
SCHEMA_VERSION = "closed-loop-bootstrap-v1"
MAX_RESPONSE_BYTES = 64 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")
STATE_LOCK_PATH = "/tmp/pilot-state.lock"


class BootstrapError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value or value != value.strip() or any(ord(char) < 32 for char in value):
        raise BootstrapError(f"{name}_INVALID")
    return value


def _credential_value(name: str, kind: str) -> str:
    raw = _required_env(name)
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise BootstrapError(f"{name}_INVALID") from None
    if (
        type(value) is not dict
        or set(value) != {"kind", "value"}
        or value.get("kind") != kind
        or type(value.get("value")) is not str
        or not value["value"]
    ):
        raise BootstrapError(f"{name}_INVALID")
    return value["value"]


def _canonical_uuid(value: object, code: str) -> str:
    if type(value) is not str:
        raise BootstrapError(code)
    try:
        canonical = str(UUID(value))
    except ValueError:
        raise BootstrapError(code) from None
    if canonical != value:
        raise BootstrapError(code)
    return value


def _base_url(name: str) -> str:
    value = _required_env(name).rstrip("/")
    allowed = {
        "PLATFORM_INTEGRATION_TB_BASE_URL": "http://tb-relay:8080",
        "PLATFORM_INTEGRATION_CMMS_BASE_URL": "http://cmms-gateway:8080",
        "CMMS_BOOTSTRAP_BASE_URL": "http://cmms-api:8080",
    }
    if value != allowed[name]:
        raise BootstrapError(f"{name}_INVALID")
    return value


def build_plan() -> dict[str, object]:
    email = _required_env("CMMS_PILOT_ADMIN_EMAIL")
    if re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+", email) is None:
        raise BootstrapError("CMMS_PILOT_ADMIN_EMAIL_INVALID")
    tenant_id = _canonical_uuid(
        _required_env("PLATFORM_INTEGRATION_TENANT_ID"), "PILOT_TENANT_ID_INVALID"
    )
    canonical: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "company_name": COMPANY_NAME,
        "admin_email": email,
        "tenant_id": tenant_id,
        "tb_base_url": _base_url("PLATFORM_INTEGRATION_TB_BASE_URL"),
        "cmms_base_url": _base_url("CMMS_BOOTSTRAP_BASE_URL"),
        "operations": [
            "verify_thingsboard_tenant_and_approver",
            "create_isolated_cmms_company_and_administrator",
            "sign_in_and_verify_cmms_company",
            "persist_local_cmms_bearer_envelope",
        ],
    }
    tb_tenant_id, tb_user_id = _tb_identity(canonical)
    canonical["tb_tenant_id"] = tb_tenant_id
    canonical["tb_approver_user_id"] = tb_user_id
    return {**canonical, "plan_sha256": _hash(canonical)}


def _password() -> str:
    path = Path("/run/secrets/cmms_admin_password")
    try:
        metadata = path.stat(follow_symlinks=False)
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        raise BootstrapError("CMMS_ADMIN_PASSWORD_UNAVAILABLE") from None
    if not stat.S_ISREG(metadata.st_mode) or not value or len(value) > 512:
        raise BootstrapError("CMMS_ADMIN_PASSWORD_INVALID")
    return value


def _request(
    base_url: str,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: object | None = None,
) -> tuple[int, object]:
    payload = None if body is None else _canonical(body)
    request_headers = {"Accept": "application/json", **(headers or {})}
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{base_url}{path}", data=payload, headers=request_headers, method=method
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        response = opener.open(request, timeout=10)
    except urllib.error.HTTPError as exc:
        response = exc
    except (OSError, urllib.error.URLError):
        raise BootstrapError("PROVIDER_UNAVAILABLE") from None
    try:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        status_code = response.status
    finally:
        response.close()
    if len(raw) > MAX_RESPONSE_BYTES:
        raise BootstrapError("PROVIDER_RESPONSE_TOO_LARGE")
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise BootstrapError("PROVIDER_RESPONSE_INVALID") from None
    return status_code, parsed


def _tb_identity(plan: dict[str, object]) -> tuple[str, str]:
    bearer = _credential_value("PILOT_TB_CREDENTIAL", "thingsboard_bearer")
    try:
        status_code, payload = _request(
            str(plan["tb_base_url"]),
            "GET",
            "/api/auth/user",
            headers={"X-Authorization": f"Bearer {bearer}"},
        )
    finally:
        del bearer
    if status_code != 200 or type(payload) is not dict:
        raise BootstrapError("THINGSBOARD_IDENTITY_FAILED")
    try:
        tenant_id = _canonical_uuid(payload["tenantId"]["id"], "TB_TENANT_ID_INVALID")
        user_id = _canonical_uuid(payload["id"]["id"], "TB_USER_ID_INVALID")
    except (KeyError, TypeError):
        raise BootstrapError("THINGSBOARD_IDENTITY_INVALID") from None
    return tenant_id, user_id


def _cmms_identity(plan: dict[str, object]) -> tuple[int, str]:
    password = _password()
    email = str(plan["admin_email"])
    signup = {
        "email": email,
        "password": password,
        "firstName": "iFactory",
        "lastName": "Pilot Admin",
        "phone": "",
        "companyName": COMPANY_NAME,
        "employeesCount": 20,
        "language": "EN",
        "timeZone": "Asia/Shanghai",
    }
    status_code, _payload = _request(
        str(plan["cmms_base_url"]), "POST", "/auth/signup", body=signup
    )
    if status_code not in {200, 201}:
        del password
        raise BootstrapError("CMMS_SIGNUP_FAILED_RESET_REQUIRED")
    status_code, auth = _request(
        str(plan["cmms_base_url"]),
        "POST",
        "/auth/signin",
        body={"email": email, "password": password, "type": "CLIENT"},
    )
    del password
    if status_code != 200 or type(auth) is not dict or type(auth.get("accessToken")) is not str:
        raise BootstrapError("CMMS_SIGNIN_FAILED_RESET_REQUIRED")
    bearer = auth["accessToken"]
    status_code, identity = _request(
        str(plan["cmms_base_url"]),
        "GET",
        "/auth/me",
        headers={"Authorization": f"Bearer {bearer}"},
    )
    if status_code != 200 or type(identity) is not dict:
        del bearer
        raise BootstrapError("CMMS_IDENTITY_FAILED_RESET_REQUIRED")
    company_id = identity.get("companyId")
    if type(company_id) is not int or company_id <= 0 or identity.get("email") != email:
        del bearer
        raise BootstrapError("CMMS_IDENTITY_MISMATCH_RESET_REQUIRED")
    return company_id, bearer


def _secure_env_file() -> Path:
    path = Path(_required_env("PILOT_ENV_FILE"))
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        raise BootstrapError("PILOT_ENV_FILE_INVALID") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
        or metadata.st_uid != os.getuid()
        or path.is_symlink()
    ):
        raise BootstrapError("PILOT_ENV_FILE_INVALID")
    return path


def _update_env(values: dict[str, str]) -> None:
    path = _secure_env_file()
    lines = path.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        if not line or line.lstrip().startswith("#") or "=" not in line:
            output.append(line)
            continue
        key = line.split("=", 1)[0]
        if key in seen:
            raise BootstrapError("PILOT_ENV_FILE_DUPLICATE_KEY")
        seen.add(key)
        output.append(f"{key}={values[key]}" if key in values else line)
    for key in sorted(values.keys() - seen):
        output.append(f"{key}={values[key]}")
    encoded = ("\n".join(output) + "\n").encode("utf-8")
    try:
        atomic_replace(path, encoded, prefix=".closed-loop-env-")
    except StateIoError:
        raise BootstrapError("PILOT_ENV_FILE_SYNC_FAILED") from None


def _runtime_root() -> Path:
    runtime = Path(_required_env("PILOT_RUNTIME_ROOT"))
    try:
        runtime_metadata = runtime.stat(follow_symlinks=False)
    except OSError:
        raise BootstrapError("PILOT_RUNTIME_ROOT_INVALID") from None
    if (
        not stat.S_ISDIR(runtime_metadata.st_mode)
        or stat.S_IMODE(runtime_metadata.st_mode) != 0o700
        or runtime_metadata.st_uid != os.getuid()
        or runtime_metadata.st_gid != os.getgid()
        or runtime.is_symlink()
    ):
        raise BootstrapError("PILOT_RUNTIME_ROOT_INVALID")
    return runtime


def _state_lock_path() -> Path:
    path = Path(_required_env("PILOT_STATE_LOCK_FILE"))
    if str(path) != STATE_LOCK_PATH:
        raise BootstrapError("PILOT_STATE_LOCK_FILE_INVALID")
    return path


def _reserve_receipt(plan_hash: str) -> tuple[int, Path]:
    runtime = _runtime_root()
    receipts = runtime / "receipts"
    receipts.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        receipts_metadata = receipts.stat(follow_symlinks=False)
    except OSError:
        raise BootstrapError("BOOTSTRAP_RECEIPT_DIR_INVALID") from None
    if (
        not stat.S_ISDIR(receipts_metadata.st_mode)
        or stat.S_IMODE(receipts_metadata.st_mode) != 0o700
        or receipts_metadata.st_uid != os.getuid()
        or receipts_metadata.st_gid != os.getgid()
        or receipts.is_symlink()
    ):
        raise BootstrapError("BOOTSTRAP_RECEIPT_DIR_INVALID")
    path = receipts / "closed-loop-bootstrap.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        raise BootstrapError("BOOTSTRAP_RECEIPT_ALREADY_EXISTS_RESET_REQUIRED") from None
    try:
        _replace_receipt(
            descriptor,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "IN_PROGRESS",
                "plan_sha256": plan_hash,
            },
        )
        fsync_directory(receipts)
    except StateIoError:
        os.close(descriptor)
        raise BootstrapError("BOOTSTRAP_RECEIPT_SYNC_FAILED") from None
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, path


def _replace_receipt(descriptor: int, value: dict[str, object]) -> None:
    payload = _canonical(value) + b"\n"
    os.ftruncate(descriptor, 0)
    os.lseek(descriptor, 0, os.SEEK_SET)
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise BootstrapError("BOOTSTRAP_RECEIPT_WRITE_FAILED")
        offset += written
    os.fsync(descriptor)


def _apply_locked(plan: dict[str, object]) -> Path:
    tb_tenant_id, tb_user_id = _tb_identity(plan)
    if tb_tenant_id != plan.get("tb_tenant_id") or tb_user_id != plan.get("tb_approver_user_id"):
        raise BootstrapError("THINGSBOARD_IDENTITY_DRIFTED")
    descriptor, receipt_path = _reserve_receipt(str(plan["plan_sha256"]))
    try:
        company_id, bearer = _cmms_identity(plan)
        try:
            envelope = json.dumps({"kind": "cmms_bearer", "value": bearer}, separators=(",", ":"))
            _update_env(
                {
                    "PLATFORM_INTEGRATION_TB_TENANT_ID": tb_tenant_id,
                    "PLATFORM_INTEGRATION_CMMS_COMPANY_ID": str(company_id),
                    "PLATFORM_INTEGRATION_APPROVER_TB_USER_ID": tb_user_id,
                    "PILOT_CMMS_CREDENTIAL": envelope,
                }
            )
        finally:
            del bearer
        _replace_receipt(
            descriptor,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "SUCCEEDED",
                "plan_sha256": plan["plan_sha256"],
                "tenant_id": plan["tenant_id"],
                "tb_tenant_id": tb_tenant_id,
                "tb_approver_user_id": tb_user_id,
                "cmms_company_id": company_id,
                "company_name": COMPANY_NAME,
            },
        )
        return receipt_path
    except BootstrapError as exc:
        _replace_receipt(
            descriptor,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "FAILED",
                "plan_sha256": plan["plan_sha256"],
                "code": exc.code,
            },
        )
        raise
    except Exception:
        _replace_receipt(
            descriptor,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "FAILED",
                "plan_sha256": plan["plan_sha256"],
                "code": "BOOTSTRAP_FAILED",
            },
        )
        raise BootstrapError("BOOTSTRAP_FAILED") from None
    finally:
        os.close(descriptor)


def apply(plan: dict[str, object], confirmed_hash: str) -> Path:
    if SHA256.fullmatch(confirmed_hash) is None or confirmed_hash != plan["plan_sha256"]:
        raise BootstrapError("BOOTSTRAP_CONFIRMATION_MISMATCH")
    try:
        with exclusive_state_lock(_state_lock_path()):
            return _apply_locked(plan)
    except StateIoError:
        raise BootstrapError("PILOT_STATE_LOCK_FAILED") from None


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("plan")
    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--confirmed-hash", required=True)
    args = parser.parse_args()
    try:
        plan = build_plan()
        if args.command == "plan":
            print(_canonical(plan).decode("utf-8"))
        else:
            receipt = apply(plan, args.confirmed_hash)
            print(json.dumps({"status": "SUCCEEDED", "receipt": str(receipt)}))
        return 0
    except BootstrapError as exc:
        print(json.dumps({"status": "FAILED", "code": exc.code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
