#!/usr/bin/env python3
"""Confirmed, at-most-once CMMS status advancement for the closed-loop pilot."""

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
import urllib.parse
import urllib.request
from uuid import UUID

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from pilot_state_io import StateIoError, exclusive_state_lock, fsync_directory  # noqa: E402


CMMS_BASE_URL = "http://cmms-gateway:8080"
EXTERNAL_SOURCE = "PDM_FORECAST"
SCHEMA_VERSION = "closed-loop-cmms-status-v1"
MAX_CREDENTIAL_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")
STATE_LOCK_PATH = "/tmp/pilot-state.lock"
TARGETS = frozenset({"IN_PROGRESS", "COMPLETE"})
STATUSES = frozenset({"OPEN", "IN_PROGRESS", "ON_HOLD", "COMPLETE"})
IDENTITY_FIELDS = (
    "work_order_id",
    "asset_id",
    "external_source",
    "external_ref",
    "correlation_id",
    "equipment_id",
    "tb_alarm_id",
    "model_profile_id",
    "model_info_id",
    "policy_version",
)


class StatusControlError(RuntimeError):
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


def _required_env(name: str, *, maximum: int = 512) -> str:
    value = os.environ.get(name, "")
    if (
        not value
        or len(value) > maximum
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise StatusControlError(f"{name}_INVALID")
    return value


def _canonical_uuid(value: object, code: str) -> str:
    if type(value) is not str:
        raise StatusControlError(code)
    try:
        canonical = str(UUID(value))
    except (ValueError, AttributeError, TypeError):
        raise StatusControlError(code) from None
    if canonical != value:
        raise StatusControlError(code)
    return value


def _positive_integer(value: object, code: str) -> int:
    if type(value) is not int or value <= 0:
        raise StatusControlError(code)
    return value


def _nonnegative_integer(value: object, code: str) -> int:
    if type(value) is not int or value < 0:
        raise StatusControlError(code)
    return value


def _bounded_text(value: object, code: str, maximum: int) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > maximum
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise StatusControlError(code)
    return value


def _strict_object(raw: str, code: str) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=reject_duplicates)
    except (json.JSONDecodeError, UnicodeError, ValueError, TypeError):
        raise StatusControlError(code) from None
    if type(value) is not dict:
        raise StatusControlError(code)
    return value


def _cmms_bearer() -> str:
    raw = _required_env("PILOT_CMMS_CREDENTIAL", maximum=MAX_CREDENTIAL_BYTES)
    envelope = _strict_object(raw, "PILOT_CMMS_CREDENTIAL_INVALID")
    if (
        set(envelope) != {"kind", "value"}
        or envelope.get("kind") != "cmms_bearer"
        or type(envelope.get("value")) is not str
    ):
        raise StatusControlError("PILOT_CMMS_CREDENTIAL_INVALID")
    bearer = str(envelope["value"])
    if (
        not bearer
        or len(bearer) > 8192
        or bearer != bearer.strip()
        or any(ord(char) < 33 or ord(char) == 127 for char in bearer)
    ):
        raise StatusControlError("PILOT_CMMS_CREDENTIAL_INVALID")
    return bearer


def _configured_base_url() -> str:
    value = _required_env("PLATFORM_INTEGRATION_CMMS_BASE_URL")
    if value != CMMS_BASE_URL:
        raise StatusControlError("PLATFORM_INTEGRATION_CMMS_BASE_URL_INVALID")
    return value


def _request(
    method: str,
    path: str,
    *,
    bearer: str,
    body: object | None = None,
) -> tuple[int, object]:
    payload = None if body is None else _canonical(body)
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {bearer}",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{CMMS_BASE_URL}{path}",
        data=payload,
        headers=headers,
        method=method,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        response = opener.open(request, timeout=10)
    except urllib.error.HTTPError as exc:
        response = exc
    except (OSError, urllib.error.URLError):
        raise StatusControlError("CMMS_PROVIDER_UNAVAILABLE") from None
    try:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        status_code = response.status
    except (OSError, urllib.error.URLError):
        raise StatusControlError("CMMS_PROVIDER_RESPONSE_LOST") from None
    finally:
        response.close()
    if len(raw) > MAX_RESPONSE_BYTES:
        raise StatusControlError("CMMS_PROVIDER_RESPONSE_TOO_LARGE")
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise StatusControlError("CMMS_PROVIDER_RESPONSE_INVALID") from None
    return status_code, parsed


def _expected_company_id() -> int:
    raw = _required_env("PLATFORM_INTEGRATION_CMMS_COMPANY_ID", maximum=20)
    try:
        parsed = int(raw)
    except ValueError:
        raise StatusControlError("CMMS_COMPANY_ID_INVALID") from None
    if parsed <= 0 or str(parsed) != raw:
        raise StatusControlError("CMMS_COMPANY_ID_INVALID")
    return parsed


def _read_company_identity(bearer: str, expected_company_id: int) -> None:
    status, payload = _request("GET", "/api/auth/me", bearer=bearer)
    if status != 200 or type(payload) is not dict:
        raise StatusControlError("CMMS_IDENTITY_FAILED")
    if payload.get("companyId") != expected_company_id:
        raise StatusControlError("CMMS_COMPANY_IDENTITY_MISMATCH")


def _snapshot(payload: object, requested_external_ref: str) -> dict[str, object]:
    if type(payload) is not dict:
        raise StatusControlError("CMMS_WORK_ORDER_RESPONSE_INVALID")
    try:
        asset = payload["asset"]
        if type(asset) is not dict:
            raise StatusControlError("CMMS_WORK_ORDER_RESPONSE_INVALID")
        status = payload["status"]
        if type(status) is not str or status not in STATUSES:
            raise StatusControlError("CMMS_WORK_ORDER_STATUS_INVALID")
        snapshot: dict[str, object] = {
            "work_order_id": _positive_integer(payload["id"], "CMMS_WORK_ORDER_ID_INVALID"),
            "asset_id": _positive_integer(asset["id"], "CMMS_ASSET_ID_INVALID"),
            "external_source": payload["external_source"],
            "external_ref": _canonical_uuid(payload["external_ref"], "CMMS_EXTERNAL_REF_INVALID"),
            "correlation_id": _canonical_uuid(
                payload["correlation_id"], "CMMS_CORRELATION_ID_INVALID"
            ),
            "equipment_id": _canonical_uuid(payload["equipment_id"], "CMMS_EQUIPMENT_ID_INVALID"),
            "tb_alarm_id": _canonical_uuid(payload["tb_alarm_id"], "CMMS_TB_ALARM_ID_INVALID"),
            "model_profile_id": _bounded_text(
                payload["model_profile_id"], "CMMS_MODEL_PROFILE_ID_INVALID", 160
            ),
            "model_info_id": _bounded_text(
                payload["model_info_id"], "CMMS_MODEL_INFO_ID_INVALID", 160
            ),
            "policy_version": _bounded_text(
                payload["policy_version"], "CMMS_POLICY_VERSION_INVALID", 100
            ),
            "current_status": status,
            "integration_event_version": _nonnegative_integer(
                payload["event_version"], "CMMS_EVENT_VERSION_INVALID"
            ),
        }
    except (KeyError, TypeError):
        raise StatusControlError("CMMS_WORK_ORDER_RESPONSE_INVALID") from None
    if snapshot["external_source"] != EXTERNAL_SOURCE:
        raise StatusControlError("CMMS_EXTERNAL_SOURCE_INVALID")
    if snapshot["external_ref"] != requested_external_ref:
        raise StatusControlError("CMMS_EXTERNAL_REF_MISMATCH")
    return snapshot


def _read_work_order(bearer: str, external_ref: str) -> dict[str, object]:
    query = urllib.parse.urlencode({"source": EXTERNAL_SOURCE, "ref": external_ref})
    status, payload = _request("GET", f"/api/work-orders/by-external-ref?{query}", bearer=bearer)
    if status != 200:
        raise StatusControlError("CMMS_WORK_ORDER_LOOKUP_FAILED")
    return _snapshot(payload, external_ref)


def _transition_allowed(current: str, target: str) -> bool:
    if current == target:
        return True
    if target == "IN_PROGRESS":
        return current in {"OPEN", "ON_HOLD"}
    return target == "COMPLETE" and current in {"OPEN", "IN_PROGRESS", "ON_HOLD"}


def build_plan(external_ref: str, target: str) -> dict[str, object]:
    canonical_external_ref = _canonical_uuid(external_ref, "EXTERNAL_REF_INVALID")
    if target not in TARGETS:
        raise StatusControlError("TARGET_STATUS_INVALID")
    tenant_id = _canonical_uuid(
        _required_env("PLATFORM_INTEGRATION_TENANT_ID"), "PILOT_TENANT_ID_INVALID"
    )
    expected_company_id = _expected_company_id()
    _configured_base_url()
    bearer = _cmms_bearer()
    try:
        _read_company_identity(bearer, expected_company_id)
        snapshot = _read_work_order(bearer, canonical_external_ref)
    finally:
        del bearer
    current = str(snapshot["current_status"])
    if not _transition_allowed(current, target):
        raise StatusControlError("CMMS_STATUS_TRANSITION_NOT_ALLOWED")
    canonical: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "operation": "advance_predictive_work_order_status",
        "tenant_id": tenant_id,
        "cmms_company_id": expected_company_id,
        **snapshot,
        "target_status": target,
        "write_required": current != target,
    }
    return {**canonical, "plan_sha256": _hash(canonical)}


def _runtime_root() -> Path:
    path = Path(_required_env("PILOT_RUNTIME_ROOT", maximum=4096))
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        raise StatusControlError("PILOT_RUNTIME_ROOT_INVALID") from None
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.getuid()
        or metadata.st_gid != os.getgid()
    ):
        raise StatusControlError("PILOT_RUNTIME_ROOT_INVALID")
    return path


def _state_lock_path() -> Path:
    path = Path(_required_env("PILOT_STATE_LOCK_FILE", maximum=4096))
    if str(path) != STATE_LOCK_PATH:
        raise StatusControlError("PILOT_STATE_LOCK_FILE_INVALID")
    return path


def _write_receipt(descriptor: int, value: dict[str, object]) -> None:
    payload = _canonical(value) + b"\n"
    os.ftruncate(descriptor, 0)
    os.lseek(descriptor, 0, os.SEEK_SET)
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise StatusControlError("CMMS_STATUS_RECEIPT_WRITE_FAILED")
        offset += written
    os.fsync(descriptor)


def _reserve_receipt(plan: dict[str, object]) -> tuple[int, str]:
    receipts = _runtime_root() / "receipts"
    try:
        receipts.mkdir(mode=0o700, parents=False, exist_ok=True)
        metadata = receipts.stat(follow_symlinks=False)
    except OSError:
        raise StatusControlError("CMMS_STATUS_RECEIPT_DIR_INVALID") from None
    if (
        receipts.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.getuid()
        or metadata.st_gid != os.getgid()
    ):
        raise StatusControlError("CMMS_STATUS_RECEIPT_DIR_INVALID")
    name = f"cmms-status-{plan['plan_sha256']}.json"
    path = receipts / name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        raise StatusControlError("CMMS_STATUS_PLAN_ALREADY_APPLIED_OR_UNCERTAIN") from None
    except OSError:
        raise StatusControlError("CMMS_STATUS_RECEIPT_CREATE_FAILED") from None
    try:
        _write_receipt(
            descriptor,
            {
                "schema_version": SCHEMA_VERSION,
                "status": "IN_PROGRESS",
                "plan_sha256": plan["plan_sha256"],
                "tenant_id": plan["tenant_id"],
                "external_ref": plan["external_ref"],
                "work_order_id": plan["work_order_id"],
                "integration_event_version": plan["integration_event_version"],
                "target_status": plan["target_status"],
            },
        )
        fsync_directory(receipts)
    except StateIoError:
        os.close(descriptor)
        raise StatusControlError("CMMS_STATUS_RECEIPT_SYNC_FAILED") from None
    except Exception:
        os.close(descriptor)
        raise
    return descriptor, name


def _same_frozen_identity(plan: dict[str, object], observed: dict[str, object]) -> bool:
    return all(plan[field] == observed[field] for field in IDENTITY_FIELDS)


def _apply_locked(external_ref: str, target: str, confirmed_hash: str) -> dict[str, object]:
    plan = build_plan(external_ref, target)
    if confirmed_hash != plan["plan_sha256"]:
        raise StatusControlError("CMMS_STATUS_CONFIRMATION_MISMATCH")
    descriptor, receipt_name = _reserve_receipt(plan)
    try:
        if not plan["write_required"]:
            result = {
                "schema_version": SCHEMA_VERSION,
                "status": "SUCCEEDED",
                "result": "ALREADY_AT_TARGET",
                "plan_sha256": plan["plan_sha256"],
                "tenant_id": plan["tenant_id"],
                "external_ref": plan["external_ref"],
                "work_order_id": plan["work_order_id"],
                "integration_event_version": plan["integration_event_version"],
                "target_status": plan["target_status"],
                "receipt": receipt_name,
            }
            _write_receipt(descriptor, result)
            return result

        bearer = _cmms_bearer()
        try:
            status, payload = _request(
                "PATCH",
                f"/api/work-orders/{plan['work_order_id']}/change-status",
                bearer=bearer,
                body={"status": plan["target_status"]},
            )
        except StatusControlError:
            raise StatusControlError("CMMS_STATUS_WRITE_OUTCOME_UNKNOWN") from None
        finally:
            del bearer
        if status != 200:
            raise StatusControlError("CMMS_STATUS_WRITE_OUTCOME_UNKNOWN")
        try:
            observed = _snapshot(payload, str(plan["external_ref"]))
        except StatusControlError:
            raise StatusControlError("CMMS_STATUS_WRITE_OUTCOME_UNKNOWN") from None
        if (
            not _same_frozen_identity(plan, observed)
            or observed["current_status"] != plan["target_status"]
            or observed["integration_event_version"] != int(plan["integration_event_version"]) + 1
        ):
            raise StatusControlError("CMMS_STATUS_WRITE_OUTCOME_UNKNOWN")
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "SUCCEEDED",
            "result": "STATUS_ADVANCED",
            "plan_sha256": plan["plan_sha256"],
            "tenant_id": plan["tenant_id"],
            "external_ref": plan["external_ref"],
            "work_order_id": plan["work_order_id"],
            "previous_status": plan["current_status"],
            "current_status": observed["current_status"],
            "integration_event_version": observed["integration_event_version"],
            "target_status": plan["target_status"],
            "receipt": receipt_name,
        }
        _write_receipt(descriptor, result)
        return result
    except StatusControlError as exc:
        outcome = {
            "schema_version": SCHEMA_VERSION,
            "status": "OUTCOME_UNKNOWN",
            "code": "CMMS_STATUS_WRITE_OUTCOME_UNKNOWN",
            "plan_sha256": plan["plan_sha256"],
            "tenant_id": plan["tenant_id"],
            "external_ref": plan["external_ref"],
            "work_order_id": plan["work_order_id"],
            "integration_event_version": plan["integration_event_version"],
            "target_status": plan["target_status"],
            "receipt": receipt_name,
        }
        try:
            _write_receipt(descriptor, outcome)
        except StatusControlError:
            pass
        if exc.code == "CMMS_STATUS_WRITE_OUTCOME_UNKNOWN":
            raise
        raise StatusControlError("CMMS_STATUS_WRITE_OUTCOME_UNKNOWN") from None
    finally:
        os.close(descriptor)


def apply(external_ref: str, target: str, confirmed_hash: str) -> dict[str, object]:
    if SHA256.fullmatch(confirmed_hash) is None:
        raise StatusControlError("CMMS_STATUS_CONFIRMATION_MISMATCH")
    try:
        with exclusive_state_lock(_state_lock_path()):
            return _apply_locked(external_ref, target, confirmed_hash)
    except StateIoError:
        raise StatusControlError("PILOT_STATE_LOCK_FAILED") from None


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "apply"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--external-ref", required=True)
        command_parser.add_argument("--target", required=True, choices=sorted(TARGETS))
        if command == "apply":
            command_parser.add_argument("--confirmed-hash", required=True)
    args = parser.parse_args()
    try:
        if args.command == "plan":
            result = build_plan(args.external_ref, args.target)
        else:
            result = apply(args.external_ref, args.target, args.confirmed_hash)
        print(_canonical(result).decode("utf-8"))
        return 0
    except StatusControlError as exc:
        print(
            json.dumps({"status": "FAILED", "code": exc.code}, separators=(",", ":")),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
