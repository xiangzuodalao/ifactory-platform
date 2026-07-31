"""Strict, host-side read-only API boundary for the isolated pilot."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from io import StringIO
from pathlib import Path
from uuid import UUID

import yaml
from dotenv import dotenv_values


_SENSITIVE = re.compile(
    r"token|password|authorization|credential|cookie|secret|apikey", re.I
)
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ALARM_ROUTE = re.compile(
    r"^/api/v2/alarm/DEVICE/"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"\?pageSize=1&page=0&statusList=ACTIVE&typeList=PDM_FORECAST_RISK$"
)
_RECEIPT_KEYS = {
    "schema_version",
    "applied_at",
    "actor",
    "plan_sha256",
    "tb_tenant_id",
    "target_count",
    "targets",
}
_MAPPING_KEYS = {
    "tb_device_id",
    "equipment_id",
    "cmms_asset_id",
    "device_type",
    "telemetry_key",
    "body_sha256",
}
_PROFILE_KEYS = {
    "CNC": "vibration_rms",
    "INJECTION_MOLDING": "injection_pressure",
    "ASSEMBLY_ROBOT": "position_deviation",
    "TIGHTENING": "torque",
    "AIR_COMPRESSOR": "discharge_pressure",
    "EOL_TESTER": "pass_rate",
}
_PROFILE_COUNTS = {
    "CNC": 4,
    "INJECTION_MOLDING": 4,
    "ASSEMBLY_ROBOT": 4,
    "TIGHTENING": 4,
    "AIR_COMPRESSOR": 2,
    "EOL_TESTER": 2,
}
_WORK_ORDER_BODY = {
    "filterFields": [],
    "direction": "ASC",
    "pageNum": 0,
    "pageSize": 1,
    "sortField": "id",
}
_MAX_JSON_BYTES = 65_536
_MAX_ENV_BYTES = 65_536
_FIXED_EXECUTABLE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_F_ADD_SEALS = 1033
_F_GET_SEALS = 1034
_REQUIRED_MEMFD_SEALS = 0x0001 | 0x0002 | 0x0004 | 0x0008
REPO_ROOT = Path(__file__).resolve().parents[3]
SHADOW_SLOT = "2026-07-29T01:15:00Z"
PILOT_TENANT_ALIAS = "ifactory-pilot"
PILOT_ACTOR = "codex-isolated-pilot-operator"
INTEGRATION_TENANT_ID = "00000000-0000-4000-8000-000000000001"
COMPOSE_FILE = REPO_ROOT / "deploy" / "compose" / "predictive-maintenance-shadow.yml"
FIXTURE_MANIFEST = (
    REPO_ROOT
    / "components"
    / "pdm-algorithm"
    / "configs"
    / "isolated_fixture_manifest.yaml"
)
_APPROVED_RUNTIME_SETTINGS = {
    "INTEGRATION_POSTGRES_DB": "ifactory_integration",
    "INTEGRATION_POSTGRES_USER": "ifactory_integration",
    "PLATFORM_INTEGRATION_TENANT_ALIAS": PILOT_TENANT_ALIAS,
    "PLATFORM_INTEGRATION_TENANT_ID": INTEGRATION_TENANT_ID,
    "PLATFORM_INTEGRATION_ISOLATED_PILOT_MODE": "1",
    "PLATFORM_INTEGRATION_PDM_BASE_URL": "http://pdm:10021",
    "PLATFORM_INTEGRATION_TB_BASE_URL": "http://host.docker.internal:8080",
    "PLATFORM_INTEGRATION_CMMS_BASE_URL": "http://host.docker.internal:3000",
    "PLATFORM_INTEGRATION_PDM_CREDENTIAL_REF": "PILOT_PDM_CREDENTIAL",
    "PLATFORM_INTEGRATION_TB_CREDENTIAL_REF": "PILOT_TB_CREDENTIAL",
    "PLATFORM_INTEGRATION_CMMS_CREDENTIAL_REF": "PILOT_CMMS_CREDENTIAL",
    "PLATFORM_INTEGRATION_CMMS_WEBHOOK_SECRET_REF": ("PILOT_CMMS_WEBHOOK_SECRET_FILE"),
    "VALEO_PDM_ALLOWED_TENANT_IDS": INTEGRATION_TENANT_ID,
}
_RUNTIME_REQUIRED = (
    *_APPROVED_RUNTIME_SETTINGS,
    "INTEGRATION_POSTGRES_PASSWORD",
    "PLATFORM_INTEGRATION_TB_TENANT_ID",
    "PLATFORM_INTEGRATION_CMMS_COMPANY_ID",
    "PILOT_PDM_CREDENTIAL",
    "PILOT_TB_CREDENTIAL",
    "PILOT_CMMS_CREDENTIAL",
    "VALEO_PDM_PREDICTION_V2_BEARER_TOKEN",
)


class PilotApiError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RuntimeEnvironmentSnapshot:
    path: Path
    device: int
    inode: int
    mode: int
    owner: int
    link_count: int
    size: int
    modified_ns: int
    changed_ns: int
    content_sha256: str

    @property
    def fingerprint(self) -> str:
        fields = (
            self.device,
            self.inode,
            self.mode,
            self.owner,
            self.link_count,
            self.size,
            self.modified_ns,
            self.changed_ns,
            self.content_sha256,
        )
        return hashlib.sha256(repr(fields).encode("ascii")).hexdigest()


@dataclass(frozen=True)
class PilotEnvironment:
    tb_base_url: str
    cmms_base_url: str
    tb_tenant_id: str
    cmms_company_id: int
    tb_bearer: str = field(repr=False)
    cmms_api_key: str = field(repr=False)
    runtime_env_path: Path | None = field(default=None, repr=False)
    runtime_env_fingerprint: str | None = field(default=None, repr=False)
    runtime_env_snapshot: RuntimeEnvironmentSnapshot | None = field(
        default=None,
        repr=False,
    )


@dataclass(frozen=True)
class SeedMapping:
    tb_device_id: str
    equipment_id: str
    cmms_asset_id: int
    device_type: str
    telemetry_key: str
    body_sha256: str


@dataclass(frozen=True)
class ConfirmedSeedReceipt:
    tb_tenant_id: str
    plan_sha256: str
    actor: str
    mappings: tuple[SeedMapping, ...]


@dataclass(frozen=True)
class ShadowAcceptanceEvidence:
    total_bindings: int
    succeeded_runs: int
    artifact_hashes: frozenset[str]
    active_pdm_alarms: int | None = None
    cmms_work_orders: int | None = None


def _strict_json_loads(raw: str | bytes, code: str) -> object:
    def object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise ValueError

    try:
        return json.loads(
            raw,
            object_pairs_hook=object_without_duplicates,
            parse_constant=reject_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        RecursionError,
    ):
        raise PilotApiError(code) from None


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError, RecursionError):
        raise PilotApiError("SEED_RECEIPT_INVALID") from None


def _runtime_snapshot(
    path: Path,
    metadata: os.stat_result,
    raw: bytes,
) -> RuntimeEnvironmentSnapshot:
    return RuntimeEnvironmentSnapshot(
        path=path,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=stat.S_IMODE(metadata.st_mode),
        owner=metadata.st_uid,
        link_count=metadata.st_nlink,
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        changed_ns=metadata.st_ctime_ns,
        content_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _read_runtime_environment(
    path: Path,
) -> tuple[bytes, RuntimeEnvironmentSnapshot]:
    absolute_path = Path(os.path.abspath(os.fspath(path)))
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(absolute_path, flags)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise PilotApiError("RUNTIME_ENV_MODE_INVALID") from None
        raise PilotApiError("RUNTIME_ENV_UNAVAILABLE") from None
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
        ):
            raise PilotApiError("RUNTIME_ENV_MODE_INVALID")
        if before.st_size > _MAX_ENV_BYTES:
            raise PilotApiError("RUNTIME_ENV_TOO_LARGE")
        chunks: list[bytes] = []
        remaining = _MAX_ENV_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
    except OSError:
        raise PilotApiError("RUNTIME_ENV_UNAVAILABLE") from None
    finally:
        os.close(descriptor)
    if len(raw) > _MAX_ENV_BYTES:
        raise PilotApiError("RUNTIME_ENV_TOO_LARGE")
    before_snapshot = _runtime_snapshot(absolute_path, before, raw)
    after_snapshot = _runtime_snapshot(absolute_path, after, raw)
    if (
        before_snapshot != after_snapshot
        or len(raw) != before.st_size
        or before.st_size != after.st_size
    ):
        raise PilotApiError("RUNTIME_ENV_CHANGED")
    return raw, before_snapshot


def _strict_lines(text: str) -> None:
    rows = text.splitlines()
    seen: set[str] = set()
    for raw in rows:
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        if value.startswith("export "):
            value = value[7:].lstrip()
        if "=" not in value:
            raise PilotApiError("RUNTIME_ENV_MALFORMED")
        key, raw_value = value.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in seen:
            raise PilotApiError("RUNTIME_ENV_MALFORMED")
        if "$" in raw_value:
            raise PilotApiError("RUNTIME_ENV_INTERPOLATION_FORBIDDEN")
        seen.add(key)


def _credential(value: object, kind: str) -> str:
    if type(value) is not str:
        raise PilotApiError("RUNTIME_CREDENTIAL_INVALID")
    envelope = _strict_json_loads(value, "RUNTIME_CREDENTIAL_INVALID")
    if type(envelope) is not dict or set(envelope) != {"kind", "value"}:
        raise PilotApiError("RUNTIME_CREDENTIAL_INVALID")
    if (
        envelope.get("kind") != kind
        or type(envelope.get("value")) is not str
        or not envelope["value"]
    ):
        raise PilotApiError("RUNTIME_CREDENTIAL_INVALID")
    return envelope["value"]


def _parse_runtime_environment(
    raw: bytes,
    snapshot: RuntimeEnvironmentSnapshot,
) -> PilotEnvironment:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise PilotApiError("RUNTIME_ENV_MALFORMED") from None
    if "\x00" in text:
        raise PilotApiError("RUNTIME_ENV_MALFORMED")
    _strict_lines(text)
    values = dotenv_values(stream=StringIO(text), interpolate=False)
    if any(
        type(values.get(key)) is not str or not values[key] for key in _RUNTIME_REQUIRED
    ):
        raise PilotApiError("RUNTIME_ENV_INCOMPLETE")
    if any(key.startswith("COMPOSE_") for key in values):
        raise PilotApiError("RUNTIME_ENV_SETTINGS_INVALID")
    if any(
        values.get(key) != expected
        for key, expected in _APPROVED_RUNTIME_SETTINGS.items()
    ):
        raise PilotApiError("RUNTIME_ENV_SETTINGS_INVALID")
    tenant = values["PLATFORM_INTEGRATION_TB_TENANT_ID"]
    if _UUID.fullmatch(tenant) is None or str(UUID(tenant)) != tenant:
        raise PilotApiError("THINGSBOARD_TENANT_ID_INVALID")
    try:
        company = int(values["PLATFORM_INTEGRATION_CMMS_COMPANY_ID"])
    except ValueError:
        raise PilotApiError("CMMS_COMPANY_ID_INVALID") from None
    if (
        str(company) != values["PLATFORM_INTEGRATION_CMMS_COMPANY_ID"]
        or not 0 < company <= 9_223_372_036_854_775_807
    ):
        raise PilotApiError("CMMS_COMPANY_ID_INVALID")
    pdm = _credential(values["PILOT_PDM_CREDENTIAL"], "opaque_bearer")
    if pdm != values["VALEO_PDM_PREDICTION_V2_BEARER_TOKEN"]:
        raise PilotApiError("PDM_TOKEN_MISMATCH")
    return PilotEnvironment(
        "http://127.0.0.1:8080",
        "http://127.0.0.1:3000",
        tenant,
        company,
        _credential(values["PILOT_TB_CREDENTIAL"], "thingsboard_bearer"),
        _credential(values["PILOT_CMMS_CREDENTIAL"], "cmms_api_key"),
        runtime_env_path=snapshot.path,
        runtime_env_fingerprint=snapshot.fingerprint,
        runtime_env_snapshot=snapshot,
    )


def load_pilot_environment(path: Path) -> PilotEnvironment:
    """Read and parse the approved runtime environment from one secure file snapshot."""
    raw, snapshot = _read_runtime_environment(path)
    return _parse_runtime_environment(raw, snapshot)


def _verified_runtime_environment_bytes(environment: PilotEnvironment) -> bytes:
    if (
        environment.runtime_env_path is None
        or environment.runtime_env_fingerprint is None
        or environment.runtime_env_snapshot is None
    ):
        raise PilotApiError("RUNTIME_ENV_SNAPSHOT_REQUIRED")
    try:
        raw, snapshot = _read_runtime_environment(environment.runtime_env_path)
        current = _parse_runtime_environment(raw, snapshot)
    except PilotApiError:
        raise PilotApiError("RUNTIME_ENV_CHANGED") from None
    if (
        snapshot != environment.runtime_env_snapshot
        or snapshot.fingerprint != environment.runtime_env_fingerprint
        or current != environment
    ):
        raise PilotApiError("RUNTIME_ENV_CHANGED")
    return raw


def _sealed_environment_descriptor(raw: bytes) -> int:
    descriptor = -1
    try:
        descriptor = os.memfd_create(
            "ifactory-phase2-compose-env",
            os.MFD_ALLOW_SEALING | os.MFD_CLOEXEC,
        )
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if type(written) is not int or written <= 0:
                raise OSError
            offset += written
        os.fchmod(descriptor, 0o400)
        os.lseek(descriptor, 0, os.SEEK_SET)
        fcntl.fcntl(
            descriptor,
            _F_ADD_SEALS,
            _REQUIRED_MEMFD_SEALS,
        )
        if (
            fcntl.fcntl(descriptor, _F_GET_SEALS) & _REQUIRED_MEMFD_SEALS
            != _REQUIRED_MEMFD_SEALS
        ):
            raise OSError
        return descriptor
    except (AttributeError, OSError):
        if descriptor >= 0:
            os.close(descriptor)
        raise PilotApiError("COMPOSE_COMMAND_FAILED") from None


def _secret_free(value: object) -> bool:
    pending = [value]
    while pending:
        current = pending.pop()
        if type(current) is dict:
            for key, item in current.items():
                if _SENSITIVE.search(str(key)) is not None:
                    return False
                pending.append(item)
        elif type(current) is list:
            pending.extend(current)
        elif type(current) is str and (
            re.search(r"(?i)\bbearer\s+\S+", current) is not None
            or re.search(r"://[^/@:\s]+:[^/@\s]+@", current) is not None
        ):
            return False
    return True


def _canonical_uuid(value: object, code: str) -> str:
    if type(value) is not str or _UUID.fullmatch(value) is None:
        raise PilotApiError(code)
    try:
        if str(UUID(value)) != value:
            raise ValueError
    except ValueError:
        raise PilotApiError(code) from None
    return value


def _file_metadata_signature(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _secure_json(path: Path) -> object:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise PilotApiError("SEED_RECEIPT_MODE_INVALID") from None
        raise PilotApiError("SEED_RECEIPT_UNAVAILABLE") from None
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
        ):
            raise PilotApiError("SEED_RECEIPT_MODE_INVALID")
        if before.st_size > _MAX_JSON_BYTES:
            raise PilotApiError("SEED_RECEIPT_INVALID")
        chunks: list[bytes] = []
        remaining = _MAX_JSON_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(descriptor)
    except OSError:
        raise PilotApiError("SEED_RECEIPT_UNAVAILABLE") from None
    finally:
        os.close(descriptor)
    if len(raw) > _MAX_JSON_BYTES:
        raise PilotApiError("SEED_RECEIPT_INVALID")
    if (
        _file_metadata_signature(before) != _file_metadata_signature(after)
        or len(raw) != before.st_size
    ):
        raise PilotApiError("SEED_RECEIPT_CHANGED")
    payload = _strict_json_loads(raw, "SEED_RECEIPT_INVALID")
    if raw != _canonical_json_bytes(payload) + b"\n":
        raise PilotApiError("SEED_RECEIPT_INVALID")
    return payload


def load_confirmed_seed_receipt(path: Path) -> ConfirmedSeedReceipt:
    """Load one immutable, secret-free receipt for the exact 20-device pilot."""
    payload = _secure_json(path)
    if not _secret_free(payload):
        raise PilotApiError("SEED_RECEIPT_SECRET_FORBIDDEN")
    if type(payload) is not dict or set(payload) != _RECEIPT_KEYS:
        raise PilotApiError("SEED_RECEIPT_INVALID")
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or type(payload["target_count"]) is not int
        or payload["target_count"] != 20
        or type(payload["plan_sha256"]) is not str
        or _SHA256.fullmatch(payload["plan_sha256"]) is None
        or type(payload["actor"]) is not str
        or payload["actor"] != PILOT_ACTOR
        or not payload["actor"].isprintable()
        or type(payload["targets"]) is not list
        or len(payload["targets"]) != 20
    ):
        raise PilotApiError("SEED_RECEIPT_INVALID")
    try:
        applied_at = datetime.fromisoformat(
            str(payload["applied_at"]).replace("Z", "+00:00")
        )
    except ValueError:
        raise PilotApiError("SEED_RECEIPT_INVALID") from None
    if applied_at.utcoffset() is None:
        raise PilotApiError("SEED_RECEIPT_INVALID")
    tenant_id = _canonical_uuid(
        payload["tb_tenant_id"],
        "SEED_RECEIPT_INVALID",
    )
    mappings: list[SeedMapping] = []
    for target in payload["targets"]:
        if type(target) is not dict or set(target) != _MAPPING_KEYS:
            raise PilotApiError("SEED_RECEIPT_MAPPING_INVALID")
        device_type = target["device_type"]
        telemetry_key = target["telemetry_key"]
        cmms_asset_id = target["cmms_asset_id"]
        body_sha256 = target["body_sha256"]
        if (
            type(device_type) is not str
            or _PROFILE_KEYS.get(device_type) != telemetry_key
            or type(cmms_asset_id) is not int
            or not 0 < cmms_asset_id <= 9_223_372_036_854_775_807
            or type(body_sha256) is not str
            or _SHA256.fullmatch(body_sha256) is None
        ):
            raise PilotApiError("SEED_RECEIPT_MAPPING_INVALID")
        mappings.append(
            SeedMapping(
                tb_device_id=_canonical_uuid(
                    target["tb_device_id"],
                    "SEED_RECEIPT_MAPPING_INVALID",
                ),
                equipment_id=_canonical_uuid(
                    target["equipment_id"],
                    "SEED_RECEIPT_MAPPING_INVALID",
                ),
                cmms_asset_id=cmms_asset_id,
                device_type=device_type,
                telemetry_key=telemetry_key,
                body_sha256=body_sha256,
            )
        )
    if (
        Counter(mapping.device_type for mapping in mappings) != _PROFILE_COUNTS
        or len({mapping.tb_device_id for mapping in mappings}) != 20
        or len({mapping.equipment_id for mapping in mappings}) != 20
        or len({mapping.cmms_asset_id for mapping in mappings}) != 20
        or len(
            {
                (
                    mapping.tb_device_id,
                    mapping.equipment_id,
                    mapping.cmms_asset_id,
                )
                for mapping in mappings
            }
        )
        != 20
    ):
        raise PilotApiError("SEED_RECEIPT_MAPPING_INVALID")
    return ConfirmedSeedReceipt(
        tb_tenant_id=tenant_id,
        plan_sha256=payload["plan_sha256"],
        actor=payload["actor"],
        mappings=tuple(sorted(mappings, key=lambda mapping: mapping.tb_device_id)),
    )


def load_fixture_artifact_hashes(
    path: Path = FIXTURE_MANIFEST,
) -> frozenset[tuple[str, str]]:
    """Read the six immutable model profile/object hashes from the tracked PDM manifest."""
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        raise PilotApiError("PDM_FIXTURE_MANIFEST_INVALID") from None
    if (
        type(payload) is not dict
        or payload.get("fixture_mode") != "isolated-pilot"
        or type(payload.get("entries")) is not list
        or len(payload["entries"]) != 6
    ):
        raise PilotApiError("PDM_FIXTURE_MANIFEST_INVALID")
    artifacts: set[tuple[str, str]] = set()
    for entry in payload["entries"]:
        if type(entry) is not dict:
            raise PilotApiError("PDM_FIXTURE_MANIFEST_INVALID")
        profile = entry.get("model_profile_id")
        artifact = entry.get("artifact_sha256")
        if (
            type(profile) is not str
            or not profile
            or type(artifact) is not str
            or _SHA256.fullmatch(artifact) is None
        ):
            raise PilotApiError("PDM_FIXTURE_MANIFEST_INVALID")
        artifacts.add((profile, artifact))
    if len(artifacts) != 6 or len({artifact for _, artifact in artifacts}) != 6:
        raise PilotApiError("PDM_FIXTURE_MANIFEST_INVALID")
    return frozenset(artifacts)


def validate_shadow_summary(
    payload: object,
    *,
    receipt: ConfirmedSeedReceipt,
    expected_artifacts: frozenset[tuple[str, str]],
) -> ShadowAcceptanceEvidence:
    """Cross-check bounded summary evidence against the confirmed seed receipt."""
    if not _secret_free(payload) or type(payload) is not dict:
        raise PilotApiError("SHADOW_SUMMARY_INVALID")
    if set(payload) != {
        "tenant_alias",
        "tenant_id",
        "scheduled_at",
        "status_counts",
        "mappings",
        "models",
    }:
        raise PilotApiError("SHADOW_SUMMARY_INVALID")
    if (
        payload["tenant_alias"] != PILOT_TENANT_ALIAS
        or payload["tenant_id"] != INTEGRATION_TENANT_ID
        or payload["scheduled_at"] != SHADOW_SLOT
        or payload["status_counts"] != {"SUCCEEDED": 20}
        or type(payload["mappings"]) is not list
        or len(payload["mappings"]) != 20
        or type(payload["models"]) is not list
        or len(payload["models"]) != 6
    ):
        raise PilotApiError("SHADOW_SUMMARY_INVALID")
    try:
        _canonical_uuid(payload["tenant_id"], "SHADOW_SUMMARY_INVALID")
    except PilotApiError:
        raise PilotApiError("SHADOW_SUMMARY_INVALID") from None
    actual_mappings: set[tuple[str, str, str, int]] = set()
    for mapping in payload["mappings"]:
        if (
            type(mapping) is not dict
            or set(mapping)
            != {"equipment_id", "meas_code", "tb_device_id", "cmms_asset_id"}
            or type(mapping["meas_code"]) is not str
            or type(mapping["cmms_asset_id"]) is not int
            or not 0 < mapping["cmms_asset_id"] <= 9_223_372_036_854_775_807
        ):
            raise PilotApiError("SHADOW_SUMMARY_INVALID")
        actual_mappings.add(
            (
                _canonical_uuid(
                    mapping["equipment_id"],
                    "SHADOW_SUMMARY_INVALID",
                ),
                mapping["meas_code"],
                _canonical_uuid(
                    mapping["tb_device_id"],
                    "SHADOW_SUMMARY_INVALID",
                ),
                mapping["cmms_asset_id"],
            )
        )
    expected_mappings = {
        (
            mapping.equipment_id,
            mapping.telemetry_key,
            mapping.tb_device_id,
            mapping.cmms_asset_id,
        )
        for mapping in receipt.mappings
    }
    if len(actual_mappings) != 20 or actual_mappings != expected_mappings:
        raise PilotApiError("SHADOW_SUMMARY_INVALID")
    normalized_artifacts: set[tuple[str, str]] = set()
    for item in expected_artifacts:
        if (
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or type(item[1]) is not str
            or _SHA256.fullmatch(item[1]) is None
        ):
            raise PilotApiError("SHADOW_SUMMARY_INVALID")
        normalized_artifacts.add(item)
    if len(normalized_artifacts) != 6:
        raise PilotApiError("SHADOW_SUMMARY_INVALID")
    actual_artifacts: set[tuple[str, str]] = set()
    for model in payload["models"]:
        if (
            type(model) is not dict
            or set(model) != {"model_profile_id", "model_artifact_sha256"}
            or type(model["model_profile_id"]) is not str
            or type(model["model_artifact_sha256"]) is not str
            or _SHA256.fullmatch(model["model_artifact_sha256"]) is None
        ):
            raise PilotApiError("SHADOW_SUMMARY_INVALID")
        actual_artifacts.add(
            (model["model_profile_id"], model["model_artifact_sha256"])
        )
    if len(actual_artifacts) != 6 or actual_artifacts != normalized_artifacts:
        raise PilotApiError("SHADOW_SUMMARY_INVALID")
    return ShadowAcceptanceEvidence(
        total_bindings=20,
        succeeded_runs=20,
        artifact_hashes=frozenset(artifact for _, artifact in actual_artifacts),
    )


class PilotCompose:
    """Run the fixed, ephemeral Phase 2 acceptance commands without a shell."""

    def __init__(self, *, environment: PilotEnvironment, runner=None) -> None:
        self._environment = environment
        self._runner = subprocess.run if runner is None else runner

    def _run(self, arguments: list[str]) -> str:
        raw_environment = _verified_runtime_environment_bytes(self._environment)
        descriptor = _sealed_environment_descriptor(raw_environment)
        command = [
            "docker",
            "--host",
            "unix:///var/run/docker.sock",
            "compose",
            "--project-name",
            "predictive-maintenance-shadow",
            "--env-file",
            f"/proc/{os.getpid()}/fd/{descriptor}",
            "-f",
            str(COMPOSE_FILE),
            *arguments,
        ]
        try:
            with tempfile.TemporaryDirectory(
                prefix="ifactory-docker-config-",
                dir="/tmp",
            ) as docker_config:
                os.chmod(docker_config, 0o700)
                result = self._runner(
                    command,
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                    shell=False,
                    timeout=180,
                    pass_fds=(descriptor,),
                    env={
                        "DOCKER_CONFIG": docker_config,
                        "PATH": _FIXED_EXECUTABLE_PATH,
                    },
                )
        except Exception:
            raise PilotApiError("COMPOSE_COMMAND_FAILED") from None
        finally:
            os.close(descriptor)
        if (
            type(getattr(result, "returncode", None)) is not int
            or result.returncode != 0
            or type(getattr(result, "stdout", None)) is not str
            or len(result.stdout.encode("utf-8")) > _MAX_JSON_BYTES
        ):
            raise PilotApiError("COMPOSE_COMMAND_FAILED")
        return result.stdout

    def collect_shadow_summary(self) -> dict[str, object]:
        self._run(
            [
                "run",
                "--rm",
                "--no-deps",
                "scheduler",
                "platform-integration",
                "scheduler",
                "--once",
                "--now",
                SHADOW_SLOT,
            ]
        )
        self._run(
            [
                "run",
                "--rm",
                "--no-deps",
                "prediction-worker",
                "platform-integration",
                "prediction-worker",
                "--once",
                "--now",
                SHADOW_SLOT,
            ]
        )
        raw = self._run(
            [
                "exec",
                "-T",
                "integration-api",
                "platform-integration",
                "shadow-summary",
                "--tenant-alias",
                PILOT_TENANT_ALIAS,
                "--scheduled-at",
                SHADOW_SLOT,
                "--format",
                "json",
            ]
        )
        payload = _strict_json_loads(raw, "SHADOW_SUMMARY_INVALID")
        if type(payload) is not dict:
            raise PilotApiError("SHADOW_SUMMARY_INVALID")
        return payload

    def collect_confirmed_shadow_summary(
        self,
        *,
        receipt: ConfirmedSeedReceipt,
    ) -> dict[str, object]:
        """Reject a stale tenant receipt before invoking the first Compose process."""
        if receipt.tb_tenant_id != self._environment.tb_tenant_id:
            raise PilotApiError("SEED_RECEIPT_TENANT_MISMATCH")
        return self.collect_shadow_summary()


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fixed_read_only_opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _RejectRedirectHandler(),
    )


def build_read_only_host_request(environment: PilotEnvironment, *, opener=None):
    """Build a host transport restricted to the 21 final isolation queries."""
    open_url = _fixed_read_only_opener().open if opener is None else opener

    def request(
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        body: object | None = None,
    ) -> tuple[int, object]:
        alarm = _ALARM_ROUTE.fullmatch(path)
        if alarm is not None:
            _canonical_uuid(alarm.group(1), "PILOT_ROUTE_FORBIDDEN")
            if (
                method != "GET"
                or body is not None
                or headers != {"X-Authorization": f"Bearer {environment.tb_bearer}"}
            ):
                raise PilotApiError("PILOT_ROUTE_FORBIDDEN")
            base_url = environment.tb_base_url
            data = None
        elif path == "/api/work-orders/search":
            if (
                method != "POST"
                or body != _WORK_ORDER_BODY
                or headers != {"x-api-key": environment.cmms_api_key}
            ):
                raise PilotApiError("PILOT_ROUTE_FORBIDDEN")
            base_url = environment.cmms_base_url
            data = json.dumps(
                body,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
            headers = {**headers, "Content-Type": "application/json"}
        else:
            raise PilotApiError("PILOT_ROUTE_FORBIDDEN")
        call = urllib.request.Request(
            base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with open_url(call, timeout=10) as response:
                raw = response.read(_MAX_JSON_BYTES + 1)
                status = response.status
        except urllib.error.HTTPError as error:
            return error.code, {}
        if type(status) is not int or len(raw) > _MAX_JSON_BYTES or not raw:
            raise PilotApiError("PILOT_PROVIDER_RESPONSE_INVALID")
        payload = _strict_json_loads(raw, "PILOT_PROVIDER_RESPONSE_INVALID")
        return status, payload

    return request


class PilotApi:
    def __init__(self, environment: PilotEnvironment, request) -> None:
        self.environment = environment
        self._request = request

    def _call(self, method: str, path: str, *, body: object | None = None) -> object:
        headers = (
            {"X-Authorization": f"Bearer {self.environment.tb_bearer}"}
            if path.startswith("/api/") and path != "/api/work-orders/search"
            else {"x-api-key": self.environment.cmms_api_key}
        )
        try:
            status, payload = self._request(method, path, headers=headers, body=body)
        except Exception:
            raise PilotApiError("PILOT_PROVIDER_UNAVAILABLE") from None
        if type(status) is not int or not 200 <= status < 300:
            raise PilotApiError("PILOT_PROVIDER_RESPONSE_INVALID")
        return payload

    def assert_clean_baseline(self, device_ids: tuple[str, ...]) -> tuple[int, int]:
        if len(device_ids) != 20 or len(set(device_ids)) != 20:
            raise PilotApiError("PILOT_TARGET_COUNT_INVALID")
        alarms = 0
        for device_id in device_ids:
            try:
                if str(UUID(device_id)) != device_id:
                    raise ValueError
            except ValueError:
                raise PilotApiError("PILOT_DEVICE_ID_INVALID") from None
            payload = self._call(
                "GET",
                f"/api/v2/alarm/DEVICE/{device_id}?pageSize=1&page=0&statusList=ACTIVE&typeList=PDM_FORECAST_RISK",
            )
            if (
                type(payload) is not dict
                or type(payload.get("totalElements")) is not int
                or payload["totalElements"] < 0
            ):
                raise PilotApiError("THINGSBOARD_ALARM_RESPONSE_INVALID")
            if payload["totalElements"] != 0:
                raise PilotApiError("ISOLATION_BASELINE_NOT_CLEAN")
        work_orders = self._call(
            "POST",
            "/api/work-orders/search",
            body={
                "filterFields": [],
                "direction": "ASC",
                "pageNum": 0,
                "pageSize": 1,
                "sortField": "id",
            },
        )
        if (
            type(work_orders) is not dict
            or type(work_orders.get("totalElements")) is not int
            or work_orders["totalElements"] < 0
        ):
            raise PilotApiError("CMMS_WORK_ORDER_RESPONSE_INVALID")
        if work_orders["totalElements"] != 0:
            raise PilotApiError("ISOLATION_BASELINE_NOT_CLEAN")
        return alarms, work_orders["totalElements"]


def evaluate_shadow_acceptance(
    payload: object,
    *,
    receipt: ConfirmedSeedReceipt,
    expected_artifacts: frozenset[tuple[str, str]],
    api: PilotApi,
) -> ShadowAcceptanceEvidence:
    """Combine bounded integration evidence with the final read-only isolation gate."""
    summary = validate_shadow_summary(
        payload,
        receipt=receipt,
        expected_artifacts=expected_artifacts,
    )
    alarms, work_orders = api.assert_clean_baseline(
        tuple(mapping.tb_device_id for mapping in receipt.mappings)
    )
    return ShadowAcceptanceEvidence(
        total_bindings=summary.total_bindings,
        succeeded_runs=summary.succeeded_runs,
        artifact_hashes=summary.artifact_hashes,
        active_pdm_alarms=alarms,
        cmms_work_orders=work_orders,
    )
