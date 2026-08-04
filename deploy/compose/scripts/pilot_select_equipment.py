#!/usr/bin/env python3
"""Read back the fixed pilot mapping and persist its deterministic equipment UUID."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
from uuid import UUID, uuid5

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from pilot_bootstrap import (  # noqa: E402
    BootstrapError,
    _base_url,
    _canonical_uuid,
    _credential_value,
    _request,
    _required_env,
    _state_lock_path,
    _update_env,
)
from pilot_state_io import (  # noqa: E402
    StateIoError,
    exclusive_state_lock,
    fsync_directory,
)


DEVICE_NAME = "LINE-A-CNC-01"
PLAN_HASH = re.compile(r"^[0-9a-f]{64}$")
SCHEMA_VERSION = "closed-loop-provision-v1"


def _write_receipt(plan_hash: str, result: dict[str, object]) -> Path:
    runtime = Path(_required_env("PILOT_RUNTIME_ROOT"))
    receipts = runtime / "receipts"
    try:
        receipts.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError:
        raise BootstrapError("PROVISION_RECEIPT_DIR_INVALID") from None
    try:
        metadata = receipts.stat(follow_symlinks=False)
    except OSError:
        raise BootstrapError("PROVISION_RECEIPT_DIR_INVALID") from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.getuid()
        or metadata.st_gid != os.getgid()
        or receipts.is_symlink()
    ):
        raise BootstrapError("PROVISION_RECEIPT_DIR_INVALID")
    path = receipts / "closed-loop-provision.json"
    payload = (
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "plan_sha256": plan_hash,
                "tenant_id": _required_env("PLATFORM_INTEGRATION_TENANT_ID"),
                "tb_tenant_id": _required_env("PLATFORM_INTEGRATION_TB_TENANT_ID"),
                "cmms_company_id": int(_required_env("PLATFORM_INTEGRATION_CMMS_COMPANY_ID")),
                **result,
                "status": "SUCCEEDED",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
    except FileExistsError:
        try:
            existing = path.read_bytes()
            metadata = path.stat(follow_symlinks=False)
        except OSError:
            raise BootstrapError("PROVISION_RECEIPT_INVALID") from None
        if (
            existing != payload
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or metadata.st_gid != os.getgid()
            or metadata.st_nlink != 1
            or path.is_symlink()
        ):
            raise BootstrapError("PROVISION_RECEIPT_CONFLICT")
        return path
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise BootstrapError("PROVISION_RECEIPT_WRITE_FAILED")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        fsync_directory(receipts)
    except StateIoError:
        raise BootstrapError("PROVISION_RECEIPT_SYNC_FAILED") from None
    return path


def _select_locked(plan_hash: str) -> dict[str, object]:
    tenant_id = UUID(
        _canonical_uuid(_required_env("PLATFORM_INTEGRATION_TENANT_ID"), "PILOT_TENANT_ID_INVALID")
    )
    expected_tb_tenant = _canonical_uuid(
        _required_env("PLATFORM_INTEGRATION_TB_TENANT_ID"), "TB_TENANT_ID_INVALID"
    )
    try:
        expected_company = int(_required_env("PLATFORM_INTEGRATION_CMMS_COMPANY_ID"))
    except ValueError:
        raise BootstrapError("CMMS_COMPANY_ID_INVALID") from None
    if expected_company <= 0:
        raise BootstrapError("CMMS_COMPANY_ID_INVALID")
    tb_base = _base_url("PLATFORM_INTEGRATION_TB_BASE_URL")
    cmms_base = _base_url("PLATFORM_INTEGRATION_CMMS_BASE_URL")
    tb_bearer = _credential_value("PILOT_TB_CREDENTIAL", "thingsboard_bearer")
    cmms_bearer = _credential_value("PILOT_CMMS_CREDENTIAL", "cmms_bearer")
    try:
        status, identity = _request(
            tb_base,
            "GET",
            "/api/auth/user",
            headers={"X-Authorization": f"Bearer {tb_bearer}"},
        )
        if status != 200 or type(identity) is not dict:
            raise BootstrapError("THINGSBOARD_IDENTITY_FAILED")
        try:
            actual_tb_tenant = _canonical_uuid(identity["tenantId"]["id"], "TB_TENANT_ID_INVALID")
        except (KeyError, TypeError):
            raise BootstrapError("THINGSBOARD_IDENTITY_INVALID") from None
        if actual_tb_tenant != expected_tb_tenant:
            raise BootstrapError("THINGSBOARD_TENANT_IDENTITY_MISMATCH")

        status, page = _request(
            tb_base,
            "GET",
            "/api/tenant/devices?pageSize=100&page=0",
            headers={"X-Authorization": f"Bearer {tb_bearer}"},
        )
        if (
            status != 200
            or type(page) is not dict
            or page.get("hasNext") is not False
            or page.get("totalElements") != 20
            or type(page.get("data")) is not list
        ):
            raise BootstrapError("PILOT_DEVICE_SET_INVALID")
        matches = [device for device in page["data"] if device.get("name") == DEVICE_NAME]
        if len(matches) != 1:
            raise BootstrapError("PILOT_WORK_ORDER_DEVICE_INVALID")
        try:
            tb_device_id = _canonical_uuid(matches[0]["id"]["id"], "TB_DEVICE_ID_INVALID")
        except (KeyError, TypeError):
            raise BootstrapError("TB_DEVICE_ID_INVALID") from None
        equipment_id = str(uuid5(tenant_id, f"pilot-equipment:{tb_device_id}"))

        status, attributes = _request(
            tb_base,
            "GET",
            f"/api/plugins/telemetry/DEVICE/{tb_device_id}/values/attributes/SERVER_SCOPE?keys=equipment_id,cmms_asset_id",
            headers={"X-Authorization": f"Bearer {tb_bearer}"},
        )
        if status != 200 or type(attributes) is not list:
            raise BootstrapError("THINGSBOARD_ATTRIBUTE_READBACK_FAILED")
        projected = {
            item.get("key"): item.get("value")
            for item in attributes
            if type(item) is dict and item.get("key") in {"equipment_id", "cmms_asset_id"}
        }
        asset_id = projected.get("cmms_asset_id")
        if (
            projected.get("equipment_id") != equipment_id
            or type(asset_id) is not int
            or asset_id <= 0
        ):
            raise BootstrapError("THINGSBOARD_ATTRIBUTE_CONFLICT")

        status, cmms_identity = _request(
            cmms_base,
            "GET",
            "/api/auth/me",
            headers={"Authorization": f"Bearer {cmms_bearer}"},
        )
        if (
            status != 200
            or type(cmms_identity) is not dict
            or cmms_identity.get("companyId") != expected_company
        ):
            raise BootstrapError("CMMS_IDENTITY_MISMATCH")
        status, asset = _request(
            cmms_base,
            "GET",
            f"/api/assets/by-equipment-id/{equipment_id}",
            headers={"Authorization": f"Bearer {cmms_bearer}"},
        )
        if (
            status != 200
            or type(asset) is not dict
            or asset.get("id") != asset_id
            or asset.get("equipment_id") != equipment_id
        ):
            raise BootstrapError("CMMS_ASSET_READBACK_CONFLICT")
    finally:
        del tb_bearer, cmms_bearer

    _update_env({"PLATFORM_INTEGRATION_PILOT_WORK_ORDER_EQUIPMENT_ID": equipment_id})
    result = {
        "status": "VERIFIED",
        "device_name": DEVICE_NAME,
        "tb_device_id": tb_device_id,
        "equipment_id": equipment_id,
        "cmms_asset_id": asset_id,
    }
    _write_receipt(plan_hash, result)
    return result


def select(plan_hash: str) -> dict[str, object]:
    if PLAN_HASH.fullmatch(plan_hash) is None:
        raise BootstrapError("PROVISION_PLAN_HASH_INVALID")
    try:
        with exclusive_state_lock(_state_lock_path()):
            return _select_locked(plan_hash)
    except StateIoError:
        raise BootstrapError("PILOT_STATE_LOCK_FAILED") from None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-hash", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(select(args.plan_hash), sort_keys=True, separators=(",", ":")))
        return 0
    except BootstrapError as exc:
        print(json.dumps({"status": "FAILED", "code": exc.code}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
