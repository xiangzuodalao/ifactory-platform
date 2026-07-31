"""Two-step, bounded shadow telemetry seed helper with no blind write retry."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from .pilot_api import PilotApiError, PilotEnvironment


START_MS = 1785283740000
END_MS = 1785287640000
END_EXCLUSIVE_MS = END_MS + 60000
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REPO_ROOT = Path(__file__).resolve().parents[3]
_MAX_PLAN_BYTES = 512 * 1024
_MAX_PROVIDER_BYTES = 256 * 1024
PROFILES = {
    "CNC": ("vibration_rms", "4.00", 2),
    "INJECTION_MOLDING": ("injection_pressure", "170.0", 1),
    "ASSEMBLY_ROBOT": ("position_deviation", "0.500", 3),
    "TIGHTENING": ("torque", "16.00", 2),
    "AIR_COMPRESSOR": ("discharge_pressure", "0.600", 3),
    "EOL_TESTER": ("pass_rate", "95.00", 2),
}
EXPECTED_DEVICES = {
    f"{line}-{kind}-{number:02d}": kind
    for line in ("LINE-A", "LINE-B")
    for kind, count in (
        ("CNC", 2),
        ("INJECTION_MOLDING", 2),
        ("ASSEMBLY_ROBOT", 2),
        ("TIGHTENING", 2),
        ("AIR_COMPRESSOR", 1),
        ("EOL_TESTER", 1),
    )
    for number in range(1, count + 1)
}
_PLAN_KEYS = {
    "schema_version",
    "created_at",
    "expires_at",
    "actor",
    "tb_tenant_id",
    "window_start",
    "window_end_exclusive",
    "targets",
    "plan_sha256",
}
_TARGET_KEYS = {
    "tb_device_id",
    "device_name",
    "device_type",
    "telemetry_key",
    "value",
    "value_scale",
    "equipment_id",
    "cmms_asset_id",
    "points",
    "body_sha256",
}
_POINT_KEYS = {"ts", "value"}


class ShadowSeedError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class WriteResultUnknown(RuntimeError):
    pass


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _secret_free(value: object) -> bool:
    if isinstance(value, dict):
        return all(
            not re.search(
                r"token|password|authorization|credential|cookie|secret|apikey",
                str(key),
                re.I,
            )
            and _secret_free(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return all(_secret_free(item) for item in value)
    return not (isinstance(value, str) and re.search(r"(?i)bearer\s+\S+", value))


def _valid_actor(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and value == value.strip()
        and len(value) <= 128
        and value.isprintable()
    )


def _canonical_uuid(value: object) -> bool:
    if type(value) is not str or re.fullmatch(r"[0-9a-f-]{36}", value) is None:
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _inode(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


class _ArtifactReservation:
    """Descriptor-bound exclusive output file reserved before provider access."""

    def __init__(self, path: Path, *, exists_code: str) -> None:
        required_flags = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW")
        if any(not hasattr(os, name) for name in required_flags):
            raise ShadowSeedError("ARTIFACT_PATH_INVALID")
        absolute = Path(os.path.abspath(path))
        if absolute.parent == absolute or not absolute.name:
            raise ShadowSeedError("ARTIFACT_PATH_INVALID")
        self._fds: list[int] = []
        self._chain: list[tuple[int, str, int]] = []
        self._parent_fd = -1
        self._file_fd = -1
        self._name = absolute.name
        self._file_inode: tuple[int, int] | None = None
        self._expected = b""
        self._committed = False
        try:
            directory_flags = (
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            )
            current_fd = os.open("/", directory_flags)
            self._fds.append(current_fd)
            for component in absolute.parts[1:-1]:
                try:
                    child_fd = os.open(
                        component,
                        directory_flags,
                        dir_fd=current_fd,
                    )
                except FileNotFoundError:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=current_fd)
                    except FileExistsError:
                        pass
                    child_fd = os.open(
                        component,
                        directory_flags,
                        dir_fd=current_fd,
                    )
                self._fds.append(child_fd)
                self._chain.append((current_fd, component, child_fd))
                current_fd = child_fd
            self._parent_fd = current_fd
            try:
                self._file_fd = os.open(
                    self._name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=self._parent_fd,
                )
            except FileExistsError:
                raise ShadowSeedError(exists_code) from None
            metadata = os.fstat(self._file_fd)
            self._file_inode = _inode(metadata)
            self._verify_file(metadata)
            self.verify()
        except ShadowSeedError:
            self.abort()
            self.close()
            raise
        except OSError:
            self.abort()
            self.close()
            raise ShadowSeedError("ARTIFACT_PATH_INVALID") from None

    def _verify_file(self, metadata: os.stat_result) -> None:
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or metadata.st_uid != os.getuid()
            or self._file_inode != _inode(metadata)
            or metadata.st_size != len(self._expected)
        ):
            raise ShadowSeedError("ARTIFACT_PATH_INVALID")

    def verify(self) -> None:
        try:
            for parent_fd, name, child_fd in self._chain:
                linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                held = os.fstat(child_fd)
                if (
                    not stat.S_ISDIR(linked.st_mode)
                    or not stat.S_ISDIR(held.st_mode)
                    or _inode(linked) != _inode(held)
                ):
                    raise ShadowSeedError("ARTIFACT_PATH_INVALID")
            linked_file = os.stat(
                self._name,
                dir_fd=self._parent_fd,
                follow_symlinks=False,
            )
            held_file = os.fstat(self._file_fd)
            if _inode(linked_file) != _inode(held_file):
                raise ShadowSeedError("ARTIFACT_PATH_INVALID")
            self._verify_file(linked_file)
            self._verify_file(held_file)
            if (
                self._expected
                and os.pread(self._file_fd, len(self._expected) + 1, 0)
                != self._expected
            ):
                raise ShadowSeedError("ARTIFACT_PATH_INVALID")
        except ShadowSeedError:
            raise
        except OSError:
            raise ShadowSeedError("ARTIFACT_PATH_INVALID") from None

    def write(self, value: dict[str, object]) -> None:
        if not _secret_free(value):
            raise ShadowSeedError("SECRET_FREE_RECEIPT_REQUIRED")
        payload = _canonical(value) + b"\n"
        self.verify()
        try:
            offset = 0
            while offset < len(payload):
                written = os.write(self._file_fd, payload[offset:])
                if written <= 0:
                    raise OSError
                offset += written
            os.fsync(self._file_fd)
            self._expected = payload
            self.verify()
        except ShadowSeedError:
            raise
        except OSError:
            raise ShadowSeedError("ARTIFACT_WRITE_FAILED") from None

    def commit(self) -> None:
        self.verify()
        self._committed = True

    def abort(self) -> None:
        if self._committed or self._parent_fd < 0 or self._file_inode is None:
            return
        try:
            linked = os.stat(
                self._name,
                dir_fd=self._parent_fd,
                follow_symlinks=False,
            )
            if _inode(linked) == self._file_inode:
                os.unlink(self._name, dir_fd=self._parent_fd)
        except OSError:
            pass

    def close(self) -> None:
        if self._file_fd >= 0:
            os.close(self._file_fd)
            self._file_fd = -1
        while self._fds:
            os.close(self._fds.pop())

    def __enter__(self) -> _ArtifactReservation:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.abort()
        self.close()


def _call(
    environment: PilotEnvironment,
    request,
    method: str,
    path: str,
    body: object | None = None,
) -> object:
    try:
        status, payload = request(
            method,
            path,
            headers={"X-Authorization": f"Bearer {environment.tb_bearer}"},
            body=body,
        )
    except TimeoutError:
        if method == "POST":
            raise WriteResultUnknown from None
        raise ShadowSeedError("THINGSBOARD_UNAVAILABLE") from None
    except WriteResultUnknown:
        raise
    except Exception:
        if method == "POST":
            raise WriteResultUnknown from None
        raise ShadowSeedError("THINGSBOARD_UNAVAILABLE") from None
    if type(status) is not int or not 200 <= status < 300:
        raise ShadowSeedError("THINGSBOARD_REQUEST_FAILED")
    return payload


def _uuid(value: object) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f-]{36}", value) is None:
        raise ShadowSeedError("THINGSBOARD_DEVICE_RESPONSE_INVALID")
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise ShadowSeedError("THINGSBOARD_DEVICE_RESPONSE_INVALID") from None
    return value


def _targets(environment: PilotEnvironment, request) -> list[dict[str, object]]:
    identity = _call(environment, request, "GET", "/api/auth/user")
    if (
        type(identity) is not dict
        or type(identity.get("tenantId")) is not dict
        or identity["tenantId"].get("id") != environment.tb_tenant_id
    ):
        raise ShadowSeedError("THINGSBOARD_TENANT_IDENTITY_MISMATCH")
    page = _call(environment, request, "GET", "/api/tenant/devices?pageSize=100&page=0")
    if (
        type(page) is not dict
        or page.get("hasNext") is not False
        or page.get("totalElements") != 20
        or type(page.get("data")) is not list
    ):
        raise ShadowSeedError("PILOT_DEVICE_SET_INVALID")
    result: list[dict[str, object]] = []
    for device in page["data"]:
        if (
            type(device) is not dict
            or type(device.get("id")) is not dict
            or device.get("name") not in EXPECTED_DEVICES
            or device.get("type") != EXPECTED_DEVICES[device.get("name")]
        ):
            raise ShadowSeedError("PILOT_DEVICE_SET_INVALID")
        device_id = _uuid(device["id"].get("id"))
        kind = device["type"]
        key, value, scale = PROFILES[kind]
        attributes = _call(
            environment,
            request,
            "GET",
            f"/api/plugins/telemetry/DEVICE/{device_id}/values/attributes/SERVER_SCOPE?keys=equipment_id,cmms_asset_id",
        )
        if type(attributes) is not list or {
            item.get("key") for item in attributes if type(item) is dict
        } != {"equipment_id", "cmms_asset_id"}:
            raise ShadowSeedError("PILOT_MAPPING_INVALID")
        mapping = {
            item["key"]: item["value"] for item in attributes if type(item) is dict
        }
        _uuid(mapping["equipment_id"])
        if (
            type(mapping["cmms_asset_id"]) is not int
            or not 0 < mapping["cmms_asset_id"] <= 9_223_372_036_854_775_807
        ):
            raise ShadowSeedError("PILOT_MAPPING_INVALID")
        result.append(
            {
                "tb_device_id": device_id,
                "device_name": device["name"],
                "device_type": kind,
                "telemetry_key": key,
                "value": value,
                "value_scale": scale,
                "equipment_id": mapping["equipment_id"],
                "cmms_asset_id": mapping["cmms_asset_id"],
            }
        )
    if (
        len(result) != 20
        or {target["device_name"] for target in result} != set(EXPECTED_DEVICES)
        or len({target["tb_device_id"] for target in result}) != 20
        or len({target["equipment_id"] for target in result}) != 20
        or len({target["cmms_asset_id"] for target in result}) != 20
    ):
        raise ShadowSeedError("PILOT_DEVICE_SET_INVALID")
    return sorted(result, key=lambda item: item["tb_device_id"])


def _window(
    environment: PilotEnvironment, request, target: dict[str, object]
) -> list[dict[str, object]]:
    key = target["telemetry_key"]
    path = f"/api/plugins/telemetry/DEVICE/{target['tb_device_id']}/values/timeseries?keys={key}&startTs={START_MS}&endTs={END_EXCLUSIVE_MS - 1}&interval=60000&agg=AVG&orderBy=ASC"
    payload = _call(environment, request, "GET", path)
    if (
        type(payload) is not dict
        or set(payload) != {key}
        or type(payload[key]) is not list
    ):
        raise ShadowSeedError("TELEMETRY_RESPONSE_INVALID")
    return payload[key]


def _body(target: dict[str, object]) -> list[dict[str, object]]:
    return [
        {"ts": timestamp, "values": {target["telemetry_key"]: float(target["value"])}}
        for timestamp in range(START_MS, END_EXCLUSIVE_MS, 60000)
    ]


def _matches(rows: list[dict[str, object]], target: dict[str, object]) -> bool:
    if len(rows) != 66:
        return False
    expected = _body(target)
    previous = None
    for row, wanted in zip(rows, expected, strict=True):
        if type(row) is not dict or row.get("ts") != wanted["ts"]:
            return False
        if previous is not None and row["ts"] <= previous:
            return False
        previous = row["ts"]
        value = row.get("value")
        try:
            actual = Decimal(str(value))
        except Exception:
            return False
        if actual != Decimal(str(target["value"])):
            return False
    return True


def build_plan(
    environment: PilotEnvironment, request, *, actor: str, output: Path, now: datetime
) -> dict[str, object]:
    if not _valid_actor(actor) or now.tzinfo is None:
        raise ShadowSeedError("SHADOW_SEED_PLAN_INVALID")
    with _ArtifactReservation(
        output,
        exists_code="SHADOW_SEED_PLAN_INVALID",
    ) as reservation:
        targets = _targets(environment, request)
        frozen: list[dict[str, object]] = []
        for target in targets:
            if _window(environment, request, target):
                raise ShadowSeedError("WINDOW_DRIFT")
            body = _body(target)
            frozen.append(
                {
                    **target,
                    "points": [
                        {"ts": row["ts"], "value": target["value"]} for row in body
                    ],
                    "body_sha256": _hash(body),
                }
            )
        plan: dict[str, object] = {
            "schema_version": 1,
            "created_at": now.astimezone(UTC).isoformat(),
            "expires_at": (now.astimezone(UTC) + timedelta(minutes=30)).isoformat(),
            "actor": actor,
            "tb_tenant_id": environment.tb_tenant_id,
            "window_start": START_MS,
            "window_end_exclusive": END_EXCLUSIVE_MS,
            "targets": frozen,
        }
        plan["plan_sha256"] = _hash(plan)
        reservation.write(plan)
        reservation.commit()
        return plan


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise ValueError("non-finite JSON number")


def _strict_json(raw: bytes) -> object:
    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )


def _read_plan(path: Path) -> bytes:
    required_flags = ("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC")
    if any(not hasattr(os, name) for name in required_flags):
        raise ShadowSeedError("SHADOW_SEED_PLAN_UNAVAILABLE")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ShadowSeedError("SHADOW_SEED_PLAN_MODE_INVALID") from None
        raise ShadowSeedError("SHADOW_SEED_PLAN_UNAVAILABLE") from None
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or before.st_uid != os.getuid()
        ):
            raise ShadowSeedError("SHADOW_SEED_PLAN_MODE_INVALID")
        if before.st_size > _MAX_PLAN_BYTES:
            raise ShadowSeedError("SHADOW_SEED_PLAN_INVALID")
        chunks: list[bytes] = []
        remaining = _MAX_PLAN_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            stat.S_IMODE(before.st_mode),
            before.st_nlink,
            before.st_uid,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            stat.S_IMODE(after.st_mode),
            after.st_nlink,
            after.st_uid,
        )
        if before_identity != after_identity or len(raw) != before.st_size:
            raise ShadowSeedError("SHADOW_SEED_PLAN_CHANGED")
    except OSError:
        raise ShadowSeedError("SHADOW_SEED_PLAN_UNAVAILABLE") from None
    finally:
        os.close(descriptor)
    if len(raw) > _MAX_PLAN_BYTES:
        raise ShadowSeedError("SHADOW_SEED_PLAN_INVALID")
    return raw


def _plan_time(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None or parsed.astimezone(UTC).isoformat() != value:
        raise ValueError
    return parsed


def _validate_plan(result: dict[str, object]) -> None:
    if (
        set(result) != _PLAN_KEYS
        or type(result["schema_version"]) is not int
        or result["schema_version"] != 1
        or not _valid_actor(result["actor"])
        or not _canonical_uuid(result["tb_tenant_id"])
        or type(result["window_start"]) is not int
        or result["window_start"] != START_MS
        or type(result["window_end_exclusive"]) is not int
        or result["window_end_exclusive"] != END_EXCLUSIVE_MS
        or type(result["plan_sha256"]) is not str
        or SHA256.fullmatch(result["plan_sha256"]) is None
        or type(result["targets"]) is not list
        or len(result["targets"]) != 20
    ):
        raise ValueError
    created = _plan_time(result["created_at"])
    expires = _plan_time(result["expires_at"])
    if expires != created + timedelta(minutes=30):
        raise ValueError
    targets: list[dict[str, object]] = []
    for target in result["targets"]:
        if type(target) is not dict or set(target) != _TARGET_KEYS:
            raise ValueError
        device_type = target["device_type"]
        profile = PROFILES.get(device_type) if type(device_type) is str else None
        if (
            profile is None
            or type(target["device_name"]) is not str
            or EXPECTED_DEVICES.get(target["device_name"]) != device_type
            or not _canonical_uuid(target["tb_device_id"])
            or not _canonical_uuid(target["equipment_id"])
            or type(target["telemetry_key"]) is not str
            or target["telemetry_key"] != profile[0]
            or type(target["value"]) is not str
            or target["value"] != profile[1]
            or type(target["value_scale"]) is not int
            or target["value_scale"] != profile[2]
            or type(target["cmms_asset_id"]) is not int
            or not 0 < target["cmms_asset_id"] <= 9_223_372_036_854_775_807
            or type(target["body_sha256"]) is not str
            or SHA256.fullmatch(target["body_sha256"]) is None
            or type(target["points"]) is not list
            or len(target["points"]) != 66
        ):
            raise ValueError
        expected_timestamp = START_MS
        for point in target["points"]:
            if (
                type(point) is not dict
                or set(point) != _POINT_KEYS
                or type(point["ts"]) is not int
                or point["ts"] != expected_timestamp
                or type(point["value"]) is not str
                or point["value"] != target["value"]
            ):
                raise ValueError
            expected_timestamp += 60_000
        if expected_timestamp != END_EXCLUSIVE_MS or target["body_sha256"] != _hash(
            _body(target)
        ):
            raise ValueError
        targets.append(target)
    if (
        [target["tb_device_id"] for target in targets]
        != sorted(target["tb_device_id"] for target in targets)
        or {target["device_name"] for target in targets} != set(EXPECTED_DEVICES)
        or len({target["tb_device_id"] for target in targets}) != 20
        or len({target["equipment_id"] for target in targets}) != 20
        or len({target["cmms_asset_id"] for target in targets}) != 20
    ):
        raise ValueError


def _load_plan(path: Path) -> dict[str, object]:
    raw = _read_plan(path)
    try:
        if not raw.endswith(b"\n"):
            raise ValueError
        result = _strict_json(raw[:-1])
        if raw != _canonical(result) + b"\n":
            raise ValueError
        if type(result) is not dict or not _secret_free(result):
            raise ValueError
        _validate_plan(result)
        actual = _hash(
            {key: value for key, value in result.items() if key != "plan_sha256"}
        )
        if result["plan_sha256"] != actual:
            raise ValueError
    except (
        KeyError,
        OverflowError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        raise ShadowSeedError("SHADOW_SEED_PLAN_INVALID") from None
    return result


def apply(
    environment: PilotEnvironment,
    request,
    *,
    plan: Path,
    plan_hash: str,
    confirmed_hash: str,
    actor: str,
    receipt: Path,
    now: datetime,
) -> dict[str, object]:
    if now.tzinfo is None:
        raise ShadowSeedError("SHADOW_SEED_TIME_INVALID")
    if not _valid_actor(actor):
        raise ShadowSeedError("SHADOW_SEED_CONFIRMATION_INVALID")
    if plan.resolve() == receipt.resolve():
        raise ShadowSeedError("PLAN_RECEIPT_ALIAS")
    if receipt.is_symlink():
        raise ShadowSeedError("ARTIFACT_PATH_INVALID")
    if SHA256.fullmatch(plan_hash) is None or plan_hash != confirmed_hash:
        raise ShadowSeedError("SHADOW_SEED_CONFIRMATION_INVALID")
    with _ArtifactReservation(
        receipt,
        exists_code="RECEIPT_ALREADY_COMPLETED",
    ) as reservation:
        saved = _load_plan(plan)
        if (
            saved["plan_sha256"] != plan_hash
            or saved.get("actor") != actor
            or saved.get("tb_tenant_id") != environment.tb_tenant_id
        ):
            raise ShadowSeedError("SHADOW_SEED_CONFIRMATION_INVALID")
        try:
            expires = datetime.fromisoformat(str(saved["expires_at"])).astimezone(UTC)
        except (KeyError, TypeError, ValueError):
            raise ShadowSeedError("SHADOW_SEED_PLAN_INVALID") from None
        if now.astimezone(UTC) >= expires:
            raise ShadowSeedError("SHADOW_SEED_PLAN_EXPIRED")
        targets = _targets(environment, request)
        planned = {target["tb_device_id"]: target for target in saved["targets"]}
        if set(planned) != {target["tb_device_id"] for target in targets}:
            raise ShadowSeedError("PILOT_MAPPING_INVALID")
        for target in targets:
            prior = planned[target["tb_device_id"]]
            if (
                any(
                    prior.get(key) != target.get(key)
                    for key in (
                        "device_name",
                        "device_type",
                        "telemetry_key",
                        "value",
                        "value_scale",
                        "equipment_id",
                        "cmms_asset_id",
                    )
                )
                or _window(environment, request, target)
                or prior.get("body_sha256") != _hash(_body(target))
            ):
                raise ShadowSeedError("WINDOW_DRIFT")
        reservation.verify()
        for target in targets:
            reservation.verify()
            path = (
                f"/api/plugins/telemetry/DEVICE/{target['tb_device_id']}/timeseries/ANY"
            )
            try:
                _call(environment, request, "POST", path, _body(target))
            except WriteResultUnknown:
                if not _matches(_window(environment, request, target), target):
                    raise ShadowSeedError("TELEMETRY_WRITE_RESULT_UNKNOWN") from None
        if not all(
            _matches(_window(environment, request, target), target)
            for target in targets
        ):
            raise ShadowSeedError("TELEMETRY_READBACK_MISMATCH")
        result: dict[str, object] = {
            "schema_version": 1,
            "applied_at": now.astimezone(UTC).isoformat(),
            "actor": actor,
            "plan_sha256": plan_hash,
            "tb_tenant_id": environment.tb_tenant_id,
            "target_count": 20,
            "targets": [
                {
                    key: target[key]
                    for key in (
                        "tb_device_id",
                        "equipment_id",
                        "cmms_asset_id",
                        "device_type",
                        "telemetry_key",
                        "body_sha256",
                    )
                }
                for target in saved["targets"]
            ],
        }
        reservation.write(result)
        reservation.commit()
        return result


def _artifact_path(value: str) -> Path:
    raw = Path(value)
    unresolved = Path.cwd() / raw if not raw.is_absolute() else raw
    lexical_runtime = Path(os.path.abspath(REPO_ROOT / ".runtime"))
    if lexical_runtime.is_symlink():
        raise ShadowSeedError("ARTIFACT_PATH_INVALID")
    lexical_candidate = Path(os.path.abspath(unresolved))
    if (
        lexical_candidate == lexical_runtime
        or lexical_runtime not in lexical_candidate.parents
    ):
        raise ShadowSeedError("ARTIFACT_PATH_INVALID")
    probe = lexical_runtime
    for part in lexical_candidate.relative_to(lexical_runtime).parts:
        probe = probe / part
        try:
            metadata = probe.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise ShadowSeedError("ARTIFACT_PATH_INVALID") from None
        if stat.S_ISLNK(metadata.st_mode) or (
            stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1
        ):
            raise ShadowSeedError("ARTIFACT_PATH_INVALID")
    runtime = lexical_runtime.resolve()
    candidate = lexical_candidate.resolve()
    if candidate == runtime or runtime not in candidate.parents:
        raise ShadowSeedError("ARTIFACT_PATH_INVALID")
    return candidate


def _host_request(environment: PilotEnvironment):
    class RejectRedirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        RejectRedirects(),
    )

    def request(
        method: str, path: str, *, headers: dict[str, str], body: object | None = None
    ) -> tuple[int, object]:
        data = None if body is None else _canonical(body)
        try:
            call = urllib.request.Request(
                environment.tb_base_url + path,
                data=data,
                headers={
                    **headers,
                    **(
                        {"Content-Type": "application/json"} if data is not None else {}
                    ),
                },
                method=method,
            )
            with opener.open(call, timeout=10) as response:
                raw = response.read(_MAX_PROVIDER_BYTES + 1)
                if len(raw) > _MAX_PROVIDER_BYTES:
                    raise ValueError("provider response is too large")
                return response.status, (_strict_json(raw) if raw else {})
        except urllib.error.HTTPError as error:
            return error.code, {}
        except (OSError, urllib.error.URLError):
            if method == "POST":
                raise WriteResultUnknown
            raise

    return request


def main(
    argv: list[str] | None = None, *, request=None, now: datetime | None = None
) -> int:
    parser = argparse.ArgumentParser(prog="shadow_seed")
    commands = parser.add_subparsers(dest="command", required=True)
    plan_parser = commands.add_parser("plan")
    plan_parser.add_argument("--env-file", required=True)
    plan_parser.add_argument("--actor", required=True)
    plan_parser.add_argument("--output", required=True)
    apply_parser = commands.add_parser("apply")
    for name in (
        "--env-file",
        "--plan",
        "--plan-hash",
        "--confirmed-hash",
        "--actor",
        "--receipt",
    ):
        apply_parser.add_argument(name, required=True)
    args = parser.parse_args(argv)
    try:
        from e2e.support.pilot_api import load_pilot_environment

        environment = load_pilot_environment(_artifact_path(args.env_file))
        current = now or datetime.now(UTC)
        transport = request or _host_request(environment)
        if args.command == "plan":
            result = build_plan(
                environment,
                transport,
                actor=args.actor,
                output=_artifact_path(args.output),
                now=current,
            )
            print(
                json.dumps(
                    {
                        "plan_sha256": result["plan_sha256"],
                        "target_body_sha256": [
                            {
                                "tb_device_id": item["tb_device_id"],
                                "body_sha256": item["body_sha256"],
                            }
                            for item in result["targets"]
                        ],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            result = apply(
                environment,
                transport,
                plan=_artifact_path(args.plan),
                plan_hash=args.plan_hash,
                confirmed_hash=args.confirmed_hash,
                actor=args.actor,
                receipt=_artifact_path(args.receipt),
                now=current,
            )
            print(
                json.dumps(
                    {
                        "plan_sha256": result["plan_sha256"],
                        "target_count": result["target_count"],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
    except (PilotApiError, ShadowSeedError) as error:
        print(f"error: {error.code}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
