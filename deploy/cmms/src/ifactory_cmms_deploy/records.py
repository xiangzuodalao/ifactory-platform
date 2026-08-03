"""Canonical CMMS deployment records and fail-closed confirmation capabilities."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import secrets
import stat
import threading
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, TypeAlias

from .errors import DeploymentError
from .secure_io import (
    RuntimePathPolicy,
    atomic_write_private,
    open_verified_parent,
    read_secure_bytes,
    require_private_regular_file,
)


JsonScalar: TypeAlias = None | bool | int | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_CODE = re.compile(r"[A-Z][A-Z0-9_.-]{0,63}\Z")
_LOGICAL_ID = re.compile(r"[a-z0-9][a-z0-9:._-]{0,127}\Z")
_UTC_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z\Z"
)
_MAX_JAVA_LONG = 9_223_372_036_854_775_807
_PLAN_MAX_BYTES = 1024 * 1024
_APPLICATION_MAX_BYTES = 64 * 1024
_CAPABILITY_TOKEN = object()

_SECRET_FIELD_NAMES = frozenset(
    {
        "password",
        "passwd",
        "token",
        "secret",
        "credential",
        "authorization",
        "cookie",
        "set_cookie",
        "api_key",
        "raw_api_key",
        "license_key",
        "jwt_secret_key",
        "private_key",
        "client_secret",
        "postgres_password",
        "minio_root_user",
        "minio_root_password",
    }
)
_SECRET_FIELD_SUFFIXES = (
    "_password",
    "_passwd",
    "_token",
    "_secret",
    "_credential",
    "_authorization",
    "_cookie",
    "_private_key",
    "_raw_key",
)
_LEGAL_SECRET_LIKE_METADATA = frozenset(
    {
        "api_key_id",
        "api_key_label",
        "api_key_capture_attempt_id",
        "api_key_capture_status",
        "api_key_capture_file",
        "api_key_capture_cleared",
        "revoked_api_key_ids",
        "license_mode",
        "license_guard_generation",
    }
)
_SECRET_VALUE_PREFIXES = (
    "cmms-test-secret://",
    "bearer ",
    "basic ",
    "-----begin ",
)
_SECRET_VALUE_MARKERS = (
    "x-amz-credential=",
    "x-amz-signature=",
    "x-amz-security-token=",
    "password=",
    "token=",
    "secret=",
    "api_key=",
    "license_key=",
)


def _invalid_record(message: str = "invalid canonical deployment record") -> DeploymentError:
    return DeploymentError("CMMS-E011", message, 11)


def _confirmation_failed() -> DeploymentError:
    return DeploymentError("CMMS-E012", "deployment plan confirmation failed", 12)


def _plan_consumed() -> DeploymentError:
    return DeploymentError("CMMS-E013", "deployment plan is already consumed", 13)


def _deployment_busy() -> DeploymentError:
    return DeploymentError("CMMS-E014", "CMMS deployment is busy", 14)


def _check_secret_field_name(name: str) -> None:
    try:
        name.encode("ascii", errors="strict")
    except UnicodeEncodeError:
        raise _invalid_record() from None
    normalized = name.lower().replace("-", "_")
    if normalized in _LEGAL_SECRET_LIKE_METADATA:
        return
    if normalized in _SECRET_FIELD_NAMES or normalized.endswith(_SECRET_FIELD_SUFFIXES):
        raise _invalid_record()


def _check_secret_string(value: str) -> None:
    lowered = value.lower()
    if value == "pls_change_me" or lowered.startswith(_SECRET_VALUE_PREFIXES):
        raise _invalid_record()
    if any(marker in lowered for marker in _SECRET_VALUE_MARKERS):
        raise _invalid_record()


def _validate_json(value: object, *, max_depth: int, depth: int = 0) -> None:
    if depth > max_depth:
        raise _invalid_record()
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is str:
        _check_secret_string(value)
        return
    if type(value) is list:
        for item in value:
            _validate_json(item, max_depth=max_depth, depth=depth + 1)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise _invalid_record()
            _check_secret_field_name(key)
            _validate_json(item, max_depth=max_depth, depth=depth + 1)
        return
    raise _invalid_record()


def canonical_json_bytes(value: JsonValue) -> bytes:
    _validate_json(value, max_depth=16)
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError, UnicodeError):
        raise _invalid_record() from None


def strict_canonical_json_loads(
    data: bytes,
    *,
    max_bytes: int,
    max_depth: int,
) -> JsonValue:
    if (
        type(data) is not bytes
        or type(max_bytes) is not int
        or isinstance(max_bytes, bool)
        or max_bytes < 0
        or type(max_depth) is not int
        or isinstance(max_depth, bool)
        or max_depth < 0
        or len(data) > max_bytes
    ):
        raise _invalid_record()
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise _invalid_record() from None

    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise _invalid_record()
            result[key] = value
        return result

    def reject_number(_value: str) -> object:
        raise _invalid_record()

    try:
        value = json.loads(
            text,
            object_pairs_hook=pairs_hook,
            parse_float=reject_number,
            parse_constant=reject_number,
        )
    except DeploymentError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError):
        raise _invalid_record() from None
    _validate_json(value, max_depth=max_depth)
    try:
        encoded = canonical_json_bytes(value)  # type: ignore[arg-type]
    except DeploymentError:
        raise
    if encoded != data:
        raise _invalid_record()
    return value  # type: ignore[return-value]


def _require_exact_keys(value: Mapping[str, object], expected: Sequence[str]) -> None:
    if type(value) is not dict or set(value) != set(expected) or len(value) != len(expected):
        raise _invalid_record()


def _require_int(value: object, *, minimum: int = 0, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise _invalid_record()
    return value


def _require_string(value: object) -> str:
    if type(value) is not str:
        raise _invalid_record()
    return value


def _require_literal(value: object, expected: JsonScalar) -> JsonScalar:
    if type(value) is not type(expected) or value != expected:
        raise _invalid_record()
    return value  # type: ignore[return-value]


def _require_pattern(value: object, pattern: re.Pattern[str]) -> str:
    text = _require_string(value)
    if pattern.fullmatch(text) is None:
        raise _invalid_record()
    return text


def _require_hex32(value: object) -> str:
    return _require_pattern(value, _HEX32)


def _require_hex40(value: object) -> str:
    return _require_pattern(value, _HEX40)


def _require_hex64(value: object) -> str:
    return _require_pattern(value, _HEX64)


def _format_utc(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise _invalid_record()
    normalized = value.astimezone(timezone.utc)
    return normalized.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_utc(value: object) -> datetime:
    text = _require_pattern(value, _UTC_TIMESTAMP)
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        raise _invalid_record() from None
    if _format_utc(parsed) != text:
        raise _invalid_record()
    return parsed


def _canonical_email(value: object) -> str:
    text = _require_string(value)
    if (
        not text
        or text != text.strip()
        or text != text.casefold()
        or "," in text
        or "@" not in text
    ):
        raise _invalid_record()
    try:
        text.encode("ascii", errors="strict")
    except UnicodeEncodeError:
        raise _invalid_record() from None
    return text


def _enum(enum_type: type[StrEnum], value: object) -> Any:
    if type(value) is not str:
        raise _invalid_record()
    try:
        return enum_type(value)
    except ValueError:
        raise _invalid_record() from None


class Operation(StrEnum):
    BOOTSTRAP = "bootstrap"
    START = "start"
    RESTART_API = "restart-api"
    RESTART_FRONTEND = "restart-frontend"
    STOP = "stop"
    REPAIR = "repair"
    SWITCH_LICENSE = "switch-license"


class RuntimeProfile(StrEnum):
    DEVELOPMENT = "development"
    ACCEPTANCE = "acceptance"


class LicenseMode(StrEnum):
    OFFLINE = "offline"
    ONLINE = "online"


class GatewayMode(StrEnum):
    STOPPED = "STOPPED"
    LOOPBACK = "LOOPBACK"
    DUAL = "DUAL"


class SourceStatus(StrEnum):
    CLEAN = "CLEAN"
    UNCOMMITTED = "UNCOMMITTED"


class CredentialIdentity(StrEnum):
    SUPER_ADMIN = "super-admin"
    ORGANIZATION_ADMIN = "organization-admin"
    RUNTIME_USER = "runtime-user"


class ApiKeyCleanupOutcome(StrEnum):
    PUBLISHED = "PUBLISHED"
    REVOKED = "REVOKED"


class BootstrapState(StrEnum):
    UNINITIALIZED = "UNINITIALIZED"
    ADMIN_ROTATED = "ADMIN_ROTATED"
    COMPANY_CREATED = "COMPANY_CREATED"
    ROLE_CREATED = "ROLE_CREATED"
    INVITATION_CREATED = "INVITATION_CREATED"
    RUNTIME_IDENTITY_CREATED = "RUNTIME_IDENTITY_CREATED"
    API_KEY_CAPTURED = "API_KEY_CAPTURED"
    FINAL_PERMISSIONS_VERIFIED = "FINAL_PERMISSIONS_VERIFIED"
    GATEWAY_ENABLED = "GATEWAY_ENABLED"


class PlanApplicationState(StrEnum):
    ATTEMPTED = "ATTEMPTED"
    CONTENDED = "CONTENDED"
    REJECTED = "REJECTED"
    IN_PROGRESS = "IN_PROGRESS"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ApplicationResultCode(StrEnum):
    APPLY_CONTENDED = "APPLY_CONTENDED"
    APPLY_REJECTED = "APPLY_REJECTED"
    APPLY_SUCCEEDED = "APPLY_SUCCEEDED"
    APPLY_FAILED = "APPLY_FAILED"


class ActionCode(StrEnum):
    GATEWAY_FAIL_CLOSED = "gateway.fail-closed"
    RUNTIME_INSTALL_CONTROL = "runtime.install-control"
    TOOLCHAIN_INSTALL = "toolchain.install"
    IMAGES_PULL_EXACT = "images.pull-exact"
    COMPOSE_CREATE_STATE_GATEWAY = "compose.create-state-gateway"
    COMPOSE_START_STATE_GATEWAY = "compose.start-state-gateway"
    COMPOSE_STOP_STATE_GATEWAY = "compose.stop-state-gateway"
    BUILD_API = "build.api"
    FRONTEND_VERIFY = "frontend.verify"
    SYSTEMD_INSTALL_UNITS = "systemd.install-units"
    LICENSE_VERIFY_OFFLINE = "license.verify-offline"
    LICENSE_DEBIT_ONLINE_START = "license.debit-online-start"
    LICENSE_RECOVER_UNKNOWN_BUDGET = "license.recover-unknown-budget"
    LICENSE_SWITCH_MODE = "license.switch-mode"
    LICENSE_STOP_GUARD = "license.stop-guard"
    PROCESS_STOP_API = "process.stop-api"
    PROCESS_STOP_FRONTEND = "process.stop-frontend"
    PROCESS_CREATE_API_PERMIT = "process.create-api-permit"
    PROCESS_START_API = "process.start-api"
    PROCESS_START_FRONTEND = "process.start-frontend"
    CMMS_INITIALIZE_FRESH_DATABASE = "cmms.initialize-fresh-database"
    BOOTSTRAP_ROTATE_SUPER_ADMIN = "bootstrap.rotate-super-admin"
    BOOTSTRAP_CREATE_ORGANIZATION = "bootstrap.create-organization"
    BOOTSTRAP_CREATE_ROLE = "bootstrap.create-role"
    BOOTSTRAP_PROBE_INVITATION = "bootstrap.probe-invitation-enforcement"
    BOOTSTRAP_CREATE_INVITATION = "bootstrap.create-invitation"
    BOOTSTRAP_CREATE_RUNTIME_IDENTITY = "bootstrap.create-runtime-identity"
    BOOTSTRAP_CREATE_API_KEY = "bootstrap.create-api-key"
    BOOTSTRAP_FINALIZE_ROLE = "bootstrap.finalize-role"
    BOOTSTRAP_PUBLISH_PHASE2_KEY = "bootstrap.publish-phase2-api-key"
    REPAIR_CAPTURE_BOOTSTRAP_DISCOVERY = "repair.capture-bootstrap-discovery"
    REPAIR_REVOKE_UNCAPTURED_API_KEY = "repair.revoke-uncaptured-api-key"
    REPAIR_FINALIZE_API_KEY_CAPTURE_CLEANUP = "repair.finalize-api-key-capture-cleanup"
    REPAIR_DISCARD_REJECTED_CANDIDATE = "repair.discard-rejected-candidate"
    REPAIR_STOP_LOOPBACK_RUNTIME = "repair.stop-loopback-runtime"
    READINESS_REQUIRE_API_LOOPBACK = "readiness.require-api-loopback"
    READINESS_REQUIRE_LOOPBACK = "readiness.require-loopback"
    GATEWAY_ENABLE_DUAL = "gateway.enable-dual"
    READINESS_REQUIRE_DUAL = "readiness.require-dual"
    READINESS_PROBE_MINIO_ROUTE = "readiness.probe-minio-route"


class ActionTargetKind(StrEnum):
    IDENTITY = "identity"
    INVITATION_PROBE_SLOT = "invitation-probe-slot"
    ROLE_EXTERNAL_ID = "role-external-id"
    API_KEY_LABEL = "api-key-label"
    API_KEY_ID = "api-key-id"
    PHASE2_ENV = "phase2-env"
    COMPOSE_RESOURCE_SET = "compose-resource-set"
    RECEIPT = "receipt"


@dataclass(frozen=True)
class SourceBinding:
    root_sha: str
    root_dirty_fingerprint: str
    root_status: SourceStatus
    cmms_gitlink: str
    cmms_head: str
    cmms_dirty_fingerprint: str
    cmms_status: SourceStatus

    def __post_init__(self) -> None:
        _require_hex40(self.root_sha)
        _require_hex64(self.root_dirty_fingerprint)
        if type(self.root_status) is not SourceStatus:
            raise _invalid_record()
        _require_hex40(self.cmms_gitlink)
        _require_hex40(self.cmms_head)
        _require_hex64(self.cmms_dirty_fingerprint)
        if type(self.cmms_status) is not SourceStatus:
            raise _invalid_record()

    def to_mapping(self) -> dict[str, JsonValue]:
        return {
            "root_sha": self.root_sha,
            "root_dirty_fingerprint": self.root_dirty_fingerprint,
            "root_status": self.root_status.value,
            "cmms_gitlink": self.cmms_gitlink,
            "cmms_head": self.cmms_head,
            "cmms_dirty_fingerprint": self.cmms_dirty_fingerprint,
            "cmms_status": self.cmms_status.value,
        }

    @classmethod
    def from_mapping(cls, value: object) -> SourceBinding:
        if type(value) is not dict:
            raise _invalid_record()
        _require_exact_keys(value, tuple(cls.__dataclass_fields__))
        return cls(
            root_sha=_require_hex40(value["root_sha"]),
            root_dirty_fingerprint=_require_hex64(value["root_dirty_fingerprint"]),
            root_status=_enum(SourceStatus, value["root_status"]),
            cmms_gitlink=_require_hex40(value["cmms_gitlink"]),
            cmms_head=_require_hex40(value["cmms_head"]),
            cmms_dirty_fingerprint=_require_hex64(value["cmms_dirty_fingerprint"]),
            cmms_status=_enum(SourceStatus, value["cmms_status"]),
        )


@dataclass(frozen=True)
class InstalledToolchain:
    """One verified, locally resolved toolchain installation."""

    name: str
    version: str
    archive_sha256: str
    home: Path = field(repr=False)

    def __post_init__(self) -> None:
        if self.name not in {"temurin", "maven", "node"}:
            raise _invalid_record()
        version = _require_string(self.version)
        if (
            not version.isascii()
            or not version.isprintable()
            or "/" in version
            or "\\" in version
            or version in {".", ".."}
        ):
            raise _invalid_record()
        _require_hex64(self.archive_sha256)
        if not isinstance(self.home, Path) or not self.home.is_absolute():
            raise _invalid_record()
        try:
            canonical = Path(os.path.abspath(os.fspath(self.home)))
        except (OSError, TypeError, ValueError):
            raise _invalid_record() from None
        if canonical != self.home:
            raise _invalid_record()


@dataclass(frozen=True)
class ToolchainReceipt:
    """In-memory receipt for the exact verified toolchain set."""

    manifest_sha256: str
    installations: tuple[InstalledToolchain, ...]

    def __post_init__(self) -> None:
        _require_hex64(self.manifest_sha256)
        if type(self.installations) is not tuple or any(
            type(row) is not InstalledToolchain for row in self.installations
        ):
            raise _invalid_record()
        if tuple(row.name for row in self.installations) != (
            "temurin",
            "maven",
            "node",
        ):
            raise _invalid_record()

    def require(self, name: str) -> InstalledToolchain:
        if type(name) is not str:
            raise _invalid_record()
        for row in self.installations:
            if row.name == name:
                return row
        raise _invalid_record()


@dataclass(frozen=True)
class BuildArtifact:
    """Verified API artifact bound to one confirmed source/toolchain snapshot."""

    source: SourceBinding
    toolchain_manifest_sha256: str
    sensitive_manifest_sha256: str
    artifact_sha256: str
    path: Path = field(repr=False)
    targeted_tests_passed: bool

    def __post_init__(self) -> None:
        if type(self.source) is not SourceBinding:
            raise _invalid_record()
        for value in (
            self.toolchain_manifest_sha256,
            self.sensitive_manifest_sha256,
            self.artifact_sha256,
        ):
            _require_hex64(value)
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise _invalid_record()
        try:
            canonical = Path(os.path.abspath(os.fspath(self.path)))
        except (OSError, TypeError, ValueError):
            raise _invalid_record() from None
        if canonical != self.path or type(self.targeted_tests_passed) is not bool:
            raise _invalid_record()


@dataclass(frozen=True)
class DependencyReceipt:
    """Verified frontend dependencies bound to one source/toolchain snapshot."""

    source: SourceBinding
    toolchain_manifest_sha256: str
    sensitive_manifest_sha256: str
    frontend_lock_sha256: str

    def __post_init__(self) -> None:
        if type(self.source) is not SourceBinding:
            raise _invalid_record()
        for value in (
            self.toolchain_manifest_sha256,
            self.sensitive_manifest_sha256,
            self.frontend_lock_sha256,
        ):
            _require_hex64(value)


@dataclass(frozen=True)
class DeploymentSnapshot:
    source: SourceBinding
    config_sha256: str
    toolchain_manifest_sha256: str
    sensitive_manifest_sha256: str
    unit_generation: str
    state_generation: int
    state_sha256: str | None

    def __post_init__(self) -> None:
        if type(self.source) is not SourceBinding:
            raise _invalid_record()
        for value in (
            self.config_sha256,
            self.toolchain_manifest_sha256,
            self.sensitive_manifest_sha256,
            self.unit_generation,
        ):
            _require_hex64(value)
        _require_int(self.state_generation)
        if self.state_generation == 0:
            if self.state_sha256 is not None:
                raise _invalid_record()
        elif self.state_generation > 0:
            _require_hex64(self.state_sha256)
        else:
            raise _invalid_record()

    def to_mapping(self) -> dict[str, JsonValue]:
        return {
            "source": self.source.to_mapping(),
            "config_sha256": self.config_sha256,
            "toolchain_manifest_sha256": self.toolchain_manifest_sha256,
            "sensitive_manifest_sha256": self.sensitive_manifest_sha256,
            "unit_generation": self.unit_generation,
            "state_generation": self.state_generation,
            "state_sha256": self.state_sha256,
        }

    @classmethod
    def from_mapping(cls, value: object) -> DeploymentSnapshot:
        if type(value) is not dict:
            raise _invalid_record()
        _require_exact_keys(value, tuple(cls.__dataclass_fields__))
        return cls(
            source=SourceBinding.from_mapping(value["source"]),
            config_sha256=_require_hex64(value["config_sha256"]),
            toolchain_manifest_sha256=_require_hex64(
                value["toolchain_manifest_sha256"]
            ),
            sensitive_manifest_sha256=_require_hex64(
                value["sensitive_manifest_sha256"]
            ),
            unit_generation=_require_hex64(value["unit_generation"]),
            state_generation=_require_int(value["state_generation"]),
            state_sha256=(
                None
                if value["state_sha256"] is None
                else _require_hex64(value["state_sha256"])
            ),
        )


@dataclass(frozen=True)
class SecureFileStatBinding:
    logical_file: str
    dev: int
    ino: int
    size: int
    mtime_ns: int
    ctime_ns: int

    def __post_init__(self) -> None:
        logical_file = _require_string(self.logical_file)
        if _LOGICAL_ID.fullmatch(logical_file) is None or "/" in logical_file:
            raise _invalid_record()
        _require_int(self.dev, minimum=1)
        _require_int(self.ino, minimum=1)
        _require_int(self.size)
        _require_int(self.mtime_ns)
        _require_int(self.ctime_ns)

    def to_mapping(self) -> dict[str, JsonValue]:
        return {
            "logical_file": self.logical_file,
            "dev": self.dev,
            "ino": self.ino,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "ctime_ns": self.ctime_ns,
        }

    @classmethod
    def from_mapping(cls, value: object) -> SecureFileStatBinding:
        if type(value) is not dict:
            raise _invalid_record()
        _require_exact_keys(value, tuple(cls.__dataclass_fields__))
        return cls(
            logical_file=_require_string(value["logical_file"]),
            dev=_require_int(value["dev"], minimum=1),
            ino=_require_int(value["ino"], minimum=1),
            size=_require_int(value["size"]),
            mtime_ns=_require_int(value["mtime_ns"]),
            ctime_ns=_require_int(value["ctime_ns"]),
        )


def _optional_stat(value: object) -> SecureFileStatBinding | None:
    return None if value is None else SecureFileStatBinding.from_mapping(value)


@dataclass(frozen=True)
class CredentialPlanBinding:
    identity: CredentialIdentity
    canonical_email: str
    current_file: SecureFileStatBinding | None
    candidate_file: SecureFileStatBinding | None

    def __post_init__(self) -> None:
        if type(self.identity) is not CredentialIdentity:
            raise _invalid_record()
        _canonical_email(self.canonical_email)
        if self.current_file is not None and type(self.current_file) is not SecureFileStatBinding:
            raise _invalid_record()
        if self.candidate_file is not None and type(self.candidate_file) is not SecureFileStatBinding:
            raise _invalid_record()

    def to_mapping(self) -> dict[str, JsonValue]:
        return {
            "identity": self.identity.value,
            "canonical_email": self.canonical_email,
            "current_file": self.current_file.to_mapping() if self.current_file else None,
            "candidate_file": self.candidate_file.to_mapping() if self.candidate_file else None,
        }

    @classmethod
    def from_mapping(cls, value: object) -> CredentialPlanBinding:
        if type(value) is not dict:
            raise _invalid_record()
        _require_exact_keys(value, tuple(cls.__dataclass_fields__))
        return cls(
            identity=_enum(CredentialIdentity, value["identity"]),
            canonical_email=_canonical_email(value["canonical_email"]),
            current_file=_optional_stat(value["current_file"]),
            candidate_file=_optional_stat(value["candidate_file"]),
        )


@dataclass(frozen=True)
class InvitationProbePlanBinding:
    slot_id: str
    canonical_email: str
    descriptor_file: SecureFileStatBinding
    password_file: SecureFileStatBinding

    def __post_init__(self) -> None:
        try:
            parsed = uuid.UUID(self.slot_id)
        except (ValueError, TypeError, AttributeError):
            raise _invalid_record() from None
        if str(parsed) != self.slot_id:
            raise _invalid_record()
        _canonical_email(self.canonical_email)
        if type(self.descriptor_file) is not SecureFileStatBinding or type(
            self.password_file
        ) is not SecureFileStatBinding:
            raise _invalid_record()

    def to_mapping(self) -> dict[str, JsonValue]:
        return {
            "slot_id": self.slot_id,
            "canonical_email": self.canonical_email,
            "descriptor_file": self.descriptor_file.to_mapping(),
            "password_file": self.password_file.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: object) -> InvitationProbePlanBinding:
        if type(value) is not dict:
            raise _invalid_record()
        _require_exact_keys(value, tuple(cls.__dataclass_fields__))
        return cls(
            slot_id=_require_string(value["slot_id"]),
            canonical_email=_canonical_email(value["canonical_email"]),
            descriptor_file=SecureFileStatBinding.from_mapping(value["descriptor_file"]),
            password_file=SecureFileStatBinding.from_mapping(value["password_file"]),
        )


@dataclass(frozen=True)
class ApiKeyCapturePlanBinding:
    attempt_id: str
    api_key_id: int
    label: str
    runtime_user_id: int
    company_id: int
    captured_file: SecureFileStatBinding

    def __post_init__(self) -> None:
        _require_hex32(self.attempt_id)
        for value in (self.api_key_id, self.runtime_user_id, self.company_id):
            _require_int(value, minimum=1, maximum=_MAX_JAVA_LONG)
        if self.label != "ifactory-pdm-runtime":
            raise _invalid_record()
        if type(self.captured_file) is not SecureFileStatBinding:
            raise _invalid_record()

    def to_mapping(self) -> dict[str, JsonValue]:
        return {
            "attempt_id": self.attempt_id,
            "api_key_id": self.api_key_id,
            "label": self.label,
            "runtime_user_id": self.runtime_user_id,
            "company_id": self.company_id,
            "captured_file": self.captured_file.to_mapping(),
        }

    @classmethod
    def from_mapping(cls, value: object) -> ApiKeyCapturePlanBinding:
        if type(value) is not dict:
            raise _invalid_record()
        _require_exact_keys(value, tuple(cls.__dataclass_fields__))
        return cls(
            attempt_id=_require_hex32(value["attempt_id"]),
            api_key_id=_require_int(value["api_key_id"], minimum=1, maximum=_MAX_JAVA_LONG),
            label=_require_string(value["label"]),
            runtime_user_id=_require_int(value["runtime_user_id"], minimum=1, maximum=_MAX_JAVA_LONG),
            company_id=_require_int(value["company_id"], minimum=1, maximum=_MAX_JAVA_LONG),
            captured_file=SecureFileStatBinding.from_mapping(value["captured_file"]),
        )


@dataclass(frozen=True)
class ApiKeyCleanupPlanBinding:
    attempt_id: str
    api_key_id: int
    terminal_outcome: ApiKeyCleanupOutcome
    historical_captured_file: SecureFileStatBinding
    observed_captured_file: SecureFileStatBinding | None
    phase2_env_file: SecureFileStatBinding | None

    def __post_init__(self) -> None:
        _require_hex32(self.attempt_id)
        _require_int(self.api_key_id, minimum=1, maximum=_MAX_JAVA_LONG)
        if type(self.terminal_outcome) is not ApiKeyCleanupOutcome:
            raise _invalid_record()
        if type(self.historical_captured_file) is not SecureFileStatBinding:
            raise _invalid_record()
        if self.observed_captured_file is not None and type(self.observed_captured_file) is not SecureFileStatBinding:
            raise _invalid_record()
        if self.phase2_env_file is not None and type(self.phase2_env_file) is not SecureFileStatBinding:
            raise _invalid_record()
        if (
            self.observed_captured_file is not None
            and self.observed_captured_file != self.historical_captured_file
        ):
            raise _invalid_record()
        if self.terminal_outcome is ApiKeyCleanupOutcome.PUBLISHED:
            if self.phase2_env_file is None:
                raise _invalid_record()
        elif self.phase2_env_file is not None:
            raise _invalid_record()

    def to_mapping(self) -> dict[str, JsonValue]:
        return {
            "attempt_id": self.attempt_id,
            "api_key_id": self.api_key_id,
            "terminal_outcome": self.terminal_outcome.value,
            "historical_captured_file": self.historical_captured_file.to_mapping(),
            "observed_captured_file": (
                self.observed_captured_file.to_mapping()
                if self.observed_captured_file
                else None
            ),
            "phase2_env_file": self.phase2_env_file.to_mapping() if self.phase2_env_file else None,
        }

    @classmethod
    def from_mapping(cls, value: object) -> ApiKeyCleanupPlanBinding:
        if type(value) is not dict:
            raise _invalid_record()
        _require_exact_keys(value, tuple(cls.__dataclass_fields__))
        return cls(
            attempt_id=_require_hex32(value["attempt_id"]),
            api_key_id=_require_int(value["api_key_id"], minimum=1, maximum=_MAX_JAVA_LONG),
            terminal_outcome=_enum(ApiKeyCleanupOutcome, value["terminal_outcome"]),
            historical_captured_file=SecureFileStatBinding.from_mapping(
                value["historical_captured_file"]
            ),
            observed_captured_file=_optional_stat(value["observed_captured_file"]),
            phase2_env_file=_optional_stat(value["phase2_env_file"]),
        )


@dataclass(frozen=True)
class BootstrapPlanBindings:
    credentials: tuple[CredentialPlanBinding, ...] | None
    invitation_probe: InvitationProbePlanBinding | None
    api_key_capture: ApiKeyCapturePlanBinding | None
    api_key_cleanup: ApiKeyCleanupPlanBinding | None
    role_external_id: str
    api_key_label: str
    phase2_env_logical_id: str
    phase2_env_file: SecureFileStatBinding | None

    def __post_init__(self) -> None:
        if (
            self.invitation_probe is not None
            and type(self.invitation_probe) is not InvitationProbePlanBinding
        ):
            raise _invalid_record()
        if (
            self.api_key_capture is not None
            and type(self.api_key_capture) is not ApiKeyCapturePlanBinding
        ):
            raise _invalid_record()
        if (
            self.api_key_cleanup is not None
            and type(self.api_key_cleanup) is not ApiKeyCleanupPlanBinding
        ):
            raise _invalid_record()
        if (
            self.phase2_env_file is not None
            and type(self.phase2_env_file) is not SecureFileStatBinding
        ):
            raise _invalid_record()
        if self.credentials is not None:
            if type(self.credentials) is not tuple or any(
                type(row) is not CredentialPlanBinding for row in self.credentials
            ):
                raise _invalid_record()
            expected = tuple(CredentialIdentity)
            actual = tuple(row.identity for row in self.credentials)
            if actual != expected or len({row.canonical_email for row in self.credentials}) != 3:
                raise _invalid_record()
            if self.api_key_cleanup is not None:
                raise _invalid_record()
        elif (
            self.api_key_cleanup is None
            or self.invitation_probe is not None
            or self.api_key_capture is not None
        ):
            raise _invalid_record()
        if self.api_key_capture is not None and self.api_key_cleanup is not None:
            raise _invalid_record()
        if self.api_key_cleanup is not None:
            if self.api_key_cleanup.terminal_outcome is ApiKeyCleanupOutcome.PUBLISHED:
                if (
                    self.phase2_env_file is None
                    or self.phase2_env_file != self.api_key_cleanup.phase2_env_file
                ):
                    raise _invalid_record()
            elif self.phase2_env_file is not None:
                raise _invalid_record()
        _require_literal(self.role_external_id, "ifactory-pdm-runtime")
        _require_literal(self.api_key_label, "ifactory-pdm-runtime")
        _require_literal(
            self.phase2_env_logical_id,
            "predictive-maintenance-shadow.env",
        )

    def to_mapping(self) -> dict[str, JsonValue]:
        return {
            "credentials": (
                [row.to_mapping() for row in self.credentials]
                if self.credentials is not None
                else None
            ),
            "invitation_probe": self.invitation_probe.to_mapping() if self.invitation_probe else None,
            "api_key_capture": self.api_key_capture.to_mapping() if self.api_key_capture else None,
            "api_key_cleanup": self.api_key_cleanup.to_mapping() if self.api_key_cleanup else None,
            "role_external_id": self.role_external_id,
            "api_key_label": self.api_key_label,
            "phase2_env_logical_id": self.phase2_env_logical_id,
            "phase2_env_file": self.phase2_env_file.to_mapping() if self.phase2_env_file else None,
        }

    @classmethod
    def from_mapping(cls, value: object) -> BootstrapPlanBindings:
        if type(value) is not dict:
            raise _invalid_record()
        _require_exact_keys(value, tuple(cls.__dataclass_fields__))
        credentials_value = value["credentials"]
        if credentials_value is not None and type(credentials_value) is not list:
            raise _invalid_record()
        return cls(
            credentials=(
                None
                if credentials_value is None
                else tuple(CredentialPlanBinding.from_mapping(row) for row in credentials_value)
            ),
            invitation_probe=(
                None
                if value["invitation_probe"] is None
                else InvitationProbePlanBinding.from_mapping(value["invitation_probe"])
            ),
            api_key_capture=(
                None
                if value["api_key_capture"] is None
                else ApiKeyCapturePlanBinding.from_mapping(value["api_key_capture"])
            ),
            api_key_cleanup=(
                None
                if value["api_key_cleanup"] is None
                else ApiKeyCleanupPlanBinding.from_mapping(value["api_key_cleanup"])
            ),
            role_external_id=_require_string(value["role_external_id"]),
            api_key_label=_require_string(value["api_key_label"]),
            phase2_env_logical_id=_require_string(value["phase2_env_logical_id"]),
            phase2_env_file=_optional_stat(value["phase2_env_file"]),
        )


@dataclass(frozen=True)
class PlannedAction:
    code: ActionCode
    target_kind: ActionTargetKind | None = None
    target_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.code) is not ActionCode:
            raise _invalid_record()
        if self.target_kind is not None and type(self.target_kind) is not ActionTargetKind:
            raise _invalid_record()
        if (self.target_kind is None) != (self.target_id is None):
            raise _invalid_record()
        if self.target_id is not None:
            _require_string(self.target_id)
            _check_secret_string(self.target_id)

    def to_mapping(self) -> dict[str, JsonValue]:
        return {
            "code": self.code.value,
            "target_kind": self.target_kind.value if self.target_kind else None,
            "target_id": self.target_id,
        }

    @classmethod
    def from_mapping(cls, value: object) -> PlannedAction:
        if type(value) is not dict:
            raise _invalid_record()
        _require_exact_keys(value, ("code", "target_kind", "target_id"))
        return cls(
            code=_enum(ActionCode, value["code"]),
            target_kind=(
                None
                if value["target_kind"] is None
                else _enum(ActionTargetKind, value["target_kind"])
            ),
            target_id=(None if value["target_id"] is None else _require_string(value["target_id"])),
        )


ACTION_RANK = (
    ActionCode.GATEWAY_FAIL_CLOSED,
    ActionCode.PROCESS_STOP_FRONTEND,
    ActionCode.PROCESS_STOP_API,
    ActionCode.LICENSE_STOP_GUARD,
    ActionCode.COMPOSE_STOP_STATE_GATEWAY,
    ActionCode.RUNTIME_INSTALL_CONTROL,
    ActionCode.TOOLCHAIN_INSTALL,
    ActionCode.IMAGES_PULL_EXACT,
    ActionCode.COMPOSE_CREATE_STATE_GATEWAY,
    ActionCode.COMPOSE_START_STATE_GATEWAY,
    ActionCode.BUILD_API,
    ActionCode.FRONTEND_VERIFY,
    ActionCode.SYSTEMD_INSTALL_UNITS,
    ActionCode.LICENSE_SWITCH_MODE,
    ActionCode.LICENSE_VERIFY_OFFLINE,
    ActionCode.LICENSE_DEBIT_ONLINE_START,
    ActionCode.LICENSE_RECOVER_UNKNOWN_BUDGET,
    ActionCode.PROCESS_CREATE_API_PERMIT,
    ActionCode.PROCESS_START_API,
    ActionCode.CMMS_INITIALIZE_FRESH_DATABASE,
    ActionCode.PROCESS_START_FRONTEND,
    ActionCode.READINESS_REQUIRE_API_LOOPBACK,
    ActionCode.REPAIR_CAPTURE_BOOTSTRAP_DISCOVERY,
    ActionCode.REPAIR_REVOKE_UNCAPTURED_API_KEY,
    ActionCode.REPAIR_DISCARD_REJECTED_CANDIDATE,
    ActionCode.BOOTSTRAP_ROTATE_SUPER_ADMIN,
    ActionCode.BOOTSTRAP_CREATE_ORGANIZATION,
    ActionCode.BOOTSTRAP_CREATE_ROLE,
    ActionCode.BOOTSTRAP_PROBE_INVITATION,
    ActionCode.BOOTSTRAP_CREATE_INVITATION,
    ActionCode.BOOTSTRAP_CREATE_RUNTIME_IDENTITY,
    ActionCode.BOOTSTRAP_CREATE_API_KEY,
    ActionCode.BOOTSTRAP_FINALIZE_ROLE,
    ActionCode.BOOTSTRAP_PUBLISH_PHASE2_KEY,
    ActionCode.REPAIR_STOP_LOOPBACK_RUNTIME,
    ActionCode.READINESS_REQUIRE_LOOPBACK,
    ActionCode.GATEWAY_ENABLE_DUAL,
    ActionCode.READINESS_REQUIRE_DUAL,
    ActionCode.READINESS_PROBE_MINIO_ROUTE,
    ActionCode.REPAIR_FINALIZE_API_KEY_CAPTURE_CLEANUP,
)
_ACTION_RANK_INDEX = MappingProxyType(
    {code: index for index, code in enumerate(ACTION_RANK)}
)


@dataclass(frozen=True)
class ActionDefinition:
    handler: str
    mutation_class: str
    allowed_operations: frozenset[Operation]
    target_policy: str
    allows_multiple_targets: bool


_TARGETED_ACTIONS = frozenset(
    {
        ActionCode.COMPOSE_CREATE_STATE_GATEWAY,
        ActionCode.COMPOSE_START_STATE_GATEWAY,
        ActionCode.COMPOSE_STOP_STATE_GATEWAY,
        ActionCode.BOOTSTRAP_ROTATE_SUPER_ADMIN,
        ActionCode.BOOTSTRAP_CREATE_ORGANIZATION,
        ActionCode.BOOTSTRAP_CREATE_ROLE,
        ActionCode.BOOTSTRAP_PROBE_INVITATION,
        ActionCode.BOOTSTRAP_CREATE_INVITATION,
        ActionCode.BOOTSTRAP_CREATE_RUNTIME_IDENTITY,
        ActionCode.BOOTSTRAP_CREATE_API_KEY,
        ActionCode.BOOTSTRAP_FINALIZE_ROLE,
        ActionCode.BOOTSTRAP_PUBLISH_PHASE2_KEY,
        ActionCode.REPAIR_CAPTURE_BOOTSTRAP_DISCOVERY,
        ActionCode.REPAIR_REVOKE_UNCAPTURED_API_KEY,
        ActionCode.REPAIR_DISCARD_REJECTED_CANDIDATE,
        ActionCode.REPAIR_FINALIZE_API_KEY_CAPTURE_CLEANUP,
    }
)
_ALLOWED_OPERATIONS = MappingProxyType(
    {
        ActionCode.GATEWAY_FAIL_CLOSED: frozenset(
            {
                Operation.BOOTSTRAP,
                Operation.START,
                Operation.RESTART_API,
                Operation.RESTART_FRONTEND,
                Operation.STOP,
                Operation.REPAIR,
                Operation.SWITCH_LICENSE,
            }
        ),
        ActionCode.PROCESS_STOP_FRONTEND: frozenset({Operation.RESTART_FRONTEND, Operation.STOP}),
        ActionCode.PROCESS_STOP_API: frozenset({Operation.RESTART_API, Operation.STOP, Operation.SWITCH_LICENSE}),
        ActionCode.LICENSE_STOP_GUARD: frozenset({Operation.RESTART_API, Operation.STOP, Operation.SWITCH_LICENSE}),
        ActionCode.COMPOSE_STOP_STATE_GATEWAY: frozenset({Operation.STOP}),
        ActionCode.RUNTIME_INSTALL_CONTROL: frozenset({Operation.BOOTSTRAP, Operation.REPAIR}),
        ActionCode.TOOLCHAIN_INSTALL: frozenset({Operation.BOOTSTRAP, Operation.REPAIR}),
        ActionCode.IMAGES_PULL_EXACT: frozenset({Operation.BOOTSTRAP, Operation.REPAIR}),
        ActionCode.COMPOSE_CREATE_STATE_GATEWAY: frozenset({Operation.BOOTSTRAP}),
        ActionCode.COMPOSE_START_STATE_GATEWAY: frozenset({Operation.START, Operation.REPAIR}),
        ActionCode.BUILD_API: frozenset({Operation.BOOTSTRAP, Operation.RESTART_API, Operation.REPAIR}),
        ActionCode.FRONTEND_VERIFY: frozenset({Operation.BOOTSTRAP, Operation.REPAIR}),
        ActionCode.SYSTEMD_INSTALL_UNITS: frozenset({Operation.BOOTSTRAP, Operation.REPAIR}),
        ActionCode.LICENSE_SWITCH_MODE: frozenset({Operation.SWITCH_LICENSE}),
        ActionCode.LICENSE_VERIFY_OFFLINE: frozenset({Operation.BOOTSTRAP, Operation.START, Operation.RESTART_API, Operation.REPAIR, Operation.SWITCH_LICENSE}),
        ActionCode.LICENSE_DEBIT_ONLINE_START: frozenset({Operation.BOOTSTRAP, Operation.START, Operation.RESTART_API, Operation.REPAIR, Operation.SWITCH_LICENSE}),
        ActionCode.LICENSE_RECOVER_UNKNOWN_BUDGET: frozenset({Operation.REPAIR}),
        ActionCode.PROCESS_CREATE_API_PERMIT: frozenset({Operation.BOOTSTRAP, Operation.START, Operation.RESTART_API, Operation.REPAIR, Operation.SWITCH_LICENSE}),
        ActionCode.PROCESS_START_API: frozenset({Operation.BOOTSTRAP, Operation.START, Operation.RESTART_API, Operation.REPAIR, Operation.SWITCH_LICENSE}),
        ActionCode.CMMS_INITIALIZE_FRESH_DATABASE: frozenset({Operation.BOOTSTRAP}),
        ActionCode.PROCESS_START_FRONTEND: frozenset({Operation.BOOTSTRAP, Operation.START, Operation.RESTART_FRONTEND, Operation.REPAIR, Operation.SWITCH_LICENSE}),
        ActionCode.READINESS_REQUIRE_API_LOOPBACK: frozenset({Operation.BOOTSTRAP, Operation.REPAIR}),
        ActionCode.REPAIR_CAPTURE_BOOTSTRAP_DISCOVERY: frozenset({Operation.REPAIR}),
        ActionCode.REPAIR_REVOKE_UNCAPTURED_API_KEY: frozenset({Operation.REPAIR}),
        ActionCode.REPAIR_DISCARD_REJECTED_CANDIDATE: frozenset({Operation.REPAIR}),
        ActionCode.BOOTSTRAP_ROTATE_SUPER_ADMIN: frozenset({Operation.BOOTSTRAP, Operation.REPAIR}),
        ActionCode.BOOTSTRAP_CREATE_ORGANIZATION: frozenset({Operation.BOOTSTRAP, Operation.REPAIR}),
        ActionCode.BOOTSTRAP_CREATE_ROLE: frozenset({Operation.BOOTSTRAP, Operation.REPAIR}),
        ActionCode.BOOTSTRAP_PROBE_INVITATION: frozenset({Operation.BOOTSTRAP, Operation.REPAIR}),
        ActionCode.BOOTSTRAP_CREATE_INVITATION: frozenset({Operation.BOOTSTRAP, Operation.REPAIR}),
        ActionCode.BOOTSTRAP_CREATE_RUNTIME_IDENTITY: frozenset({Operation.BOOTSTRAP, Operation.REPAIR}),
        ActionCode.BOOTSTRAP_CREATE_API_KEY: frozenset({Operation.BOOTSTRAP, Operation.REPAIR}),
        ActionCode.BOOTSTRAP_FINALIZE_ROLE: frozenset({Operation.BOOTSTRAP, Operation.REPAIR}),
        ActionCode.BOOTSTRAP_PUBLISH_PHASE2_KEY: frozenset({Operation.BOOTSTRAP, Operation.REPAIR}),
        ActionCode.REPAIR_STOP_LOOPBACK_RUNTIME: frozenset({Operation.REPAIR}),
        ActionCode.READINESS_REQUIRE_LOOPBACK: frozenset({Operation.BOOTSTRAP, Operation.START, Operation.RESTART_API, Operation.RESTART_FRONTEND, Operation.REPAIR, Operation.SWITCH_LICENSE}),
        ActionCode.GATEWAY_ENABLE_DUAL: frozenset({Operation.BOOTSTRAP, Operation.START, Operation.RESTART_API, Operation.RESTART_FRONTEND, Operation.REPAIR, Operation.SWITCH_LICENSE}),
        ActionCode.READINESS_REQUIRE_DUAL: frozenset({Operation.BOOTSTRAP, Operation.START, Operation.RESTART_API, Operation.RESTART_FRONTEND, Operation.REPAIR, Operation.SWITCH_LICENSE}),
        ActionCode.READINESS_PROBE_MINIO_ROUTE: frozenset({Operation.BOOTSTRAP, Operation.REPAIR}),
        ActionCode.REPAIR_FINALIZE_API_KEY_CAPTURE_CLEANUP: frozenset({Operation.REPAIR}),
    }
)
_ACTION_DEFINITIONS = MappingProxyType(
    {
        code: ActionDefinition(
            handler=code.value.replace(".", "_"),
            mutation_class=code.value.split(".", 1)[0],
            allowed_operations=_ALLOWED_OPERATIONS[code],
            target_policy="required" if code in _TARGETED_ACTIONS else "forbidden",
            allows_multiple_targets=False,
        )
        for code in ActionCode
    }
)


def _a(code: ActionCode) -> PlannedAction:
    return PlannedAction(code)


def _validate_targets(
    rows: tuple[PlannedAction, ...], bindings: BootstrapPlanBindings | None
) -> None:
    slot_id = bindings.invitation_probe.slot_id if bindings and bindings.invitation_probe else None
    exact = {
        ActionCode.COMPOSE_CREATE_STATE_GATEWAY: (ActionTargetKind.COMPOSE_RESOURCE_SET, "compose:ifactory-cmms-dev"),
        ActionCode.COMPOSE_START_STATE_GATEWAY: (ActionTargetKind.COMPOSE_RESOURCE_SET, "compose:ifactory-cmms-dev"),
        ActionCode.COMPOSE_STOP_STATE_GATEWAY: (ActionTargetKind.COMPOSE_RESOURCE_SET, "compose:ifactory-cmms-dev"),
        ActionCode.BOOTSTRAP_ROTATE_SUPER_ADMIN: (ActionTargetKind.IDENTITY, "super-admin"),
        ActionCode.BOOTSTRAP_CREATE_ORGANIZATION: (ActionTargetKind.IDENTITY, "organization-admin"),
        ActionCode.BOOTSTRAP_CREATE_INVITATION: (ActionTargetKind.IDENTITY, "runtime-user"),
        ActionCode.BOOTSTRAP_CREATE_RUNTIME_IDENTITY: (ActionTargetKind.IDENTITY, "runtime-user"),
        ActionCode.BOOTSTRAP_CREATE_ROLE: (ActionTargetKind.ROLE_EXTERNAL_ID, "ifactory-pdm-runtime"),
        ActionCode.BOOTSTRAP_FINALIZE_ROLE: (ActionTargetKind.ROLE_EXTERNAL_ID, "ifactory-pdm-runtime"),
        ActionCode.BOOTSTRAP_CREATE_API_KEY: (ActionTargetKind.API_KEY_LABEL, "ifactory-pdm-runtime"),
        ActionCode.BOOTSTRAP_PUBLISH_PHASE2_KEY: (ActionTargetKind.PHASE2_ENV, "predictive-maintenance-shadow.env"),
        ActionCode.REPAIR_CAPTURE_BOOTSTRAP_DISCOVERY: (ActionTargetKind.RECEIPT, "receipt:cmms-bootstrap"),
        ActionCode.REPAIR_FINALIZE_API_KEY_CAPTURE_CLEANUP: (ActionTargetKind.RECEIPT, "receipt:cmms-bootstrap"),
    }
    if slot_id:
        exact[ActionCode.BOOTSTRAP_PROBE_INVITATION] = (
            ActionTargetKind.INVITATION_PROBE_SLOT,
            slot_id,
        )
    for row in rows:
        if row.code not in _TARGETED_ACTIONS:
            if row.target_kind is not None:
                raise _invalid_record()
        elif row.code is ActionCode.REPAIR_REVOKE_UNCAPTURED_API_KEY:
            target = row.target_id or ""
            if row.target_kind is not ActionTargetKind.API_KEY_ID or not target.isascii() or not target.isdigit() or target.startswith("0"):
                raise _invalid_record()
            _require_int(int(target), minimum=1, maximum=_MAX_JAVA_LONG)
        elif row.code is ActionCode.REPAIR_DISCARD_REJECTED_CANDIDATE:
            if row.target_kind is not ActionTargetKind.IDENTITY or row.target_id not in {item.value for item in CredentialIdentity}:
                raise _invalid_record()
        elif exact.get(row.code) != (row.target_kind, row.target_id):
            raise _invalid_record()


_I_BOOTSTRAP = frozenset(
    {
        ActionCode.RUNTIME_INSTALL_CONTROL,
        ActionCode.TOOLCHAIN_INSTALL,
        ActionCode.IMAGES_PULL_EXACT,
        ActionCode.BUILD_API,
        ActionCode.FRONTEND_VERIFY,
        ActionCode.SYSTEMD_INSTALL_UNITS,
    }
)
_I_REPAIR_API = frozenset(
    {
        ActionCode.RUNTIME_INSTALL_CONTROL,
        ActionCode.TOOLCHAIN_INSTALL,
        ActionCode.IMAGES_PULL_EXACT,
        ActionCode.COMPOSE_START_STATE_GATEWAY,
        ActionCode.BUILD_API,
        ActionCode.SYSTEMD_INSTALL_UNITS,
    }
)
_I_REPAIR_FULL = _I_REPAIR_API | {ActionCode.FRONTEND_VERIFY}
_C_FULL_CODES = (
    ActionCode.BOOTSTRAP_ROTATE_SUPER_ADMIN,
    ActionCode.BOOTSTRAP_CREATE_ORGANIZATION,
    ActionCode.BOOTSTRAP_CREATE_ROLE,
    ActionCode.BOOTSTRAP_PROBE_INVITATION,
    ActionCode.BOOTSTRAP_CREATE_INVITATION,
    ActionCode.BOOTSTRAP_CREATE_RUNTIME_IDENTITY,
    ActionCode.BOOTSTRAP_CREATE_API_KEY,
    ActionCode.BOOTSTRAP_FINALIZE_ROLE,
    ActionCode.BOOTSTRAP_PUBLISH_PHASE2_KEY,
)
_C_PROBE_RESOLVED_CODES = tuple(
    code for code in _C_FULL_CODES if code is not ActionCode.BOOTSTRAP_PROBE_INVITATION
)
_C_CODE_SET = frozenset(_C_FULL_CODES)


def _matches_codes(
    codes: frozenset[ActionCode],
    required: set[ActionCode],
    optional: frozenset[ActionCode] = frozenset(),
) -> bool:
    return required.issubset(codes) and codes.issubset(required | optional)


def _legal_completion_suffix(codes: tuple[ActionCode, ...]) -> bool:
    if not codes or codes[-1] is not ActionCode.BOOTSTRAP_PUBLISH_PHASE2_KEY:
        return False
    return any(
        codes == language[start:]
        for language in (_C_FULL_CODES, _C_PROBE_RESOLVED_CODES)
        for start in range(len(language))
    )


_IDENTITY_MUTATION_ACTION = MappingProxyType(
    {
        CredentialIdentity.SUPER_ADMIN: ActionCode.BOOTSTRAP_ROTATE_SUPER_ADMIN,
        CredentialIdentity.ORGANIZATION_ADMIN: ActionCode.BOOTSTRAP_CREATE_ORGANIZATION,
        CredentialIdentity.RUNTIME_USER: ActionCode.BOOTSTRAP_CREATE_RUNTIME_IDENTITY,
    }
)
_COMPLETE_IDENTITY_SET = frozenset(CredentialIdentity)
_COMPLETE_BINDING_BRANCHES = frozenset(
    {
        "start-active",
        "start-stopped",
        "restart-api",
        "restart-frontend",
        "switch-license",
        "repair-readiness",
        "repair-budget",
    }
)


def _require_credential_projection(
    bindings: BootstrapPlanBindings,
    *,
    current: frozenset[CredentialIdentity],
    candidate: frozenset[CredentialIdentity],
) -> None:
    if bindings.credentials is None:
        raise _invalid_record()
    for row in bindings.credentials:
        if (row.current_file is not None) != (row.identity in current):
            raise _invalid_record()
        if (row.candidate_file is not None) != (row.identity in candidate):
            raise _invalid_record()


def _validate_bootstrap_bindings(
    bindings: BootstrapPlanBindings | None,
    *,
    branch: str,
    rows: tuple[PlannedAction, ...],
) -> None:
    if branch == "stop":
        if bindings is not None:
            raise _invalid_record()
        return
    if type(bindings) is not BootstrapPlanBindings:
        raise _invalid_record()
    if branch == "repair-cleanup":
        cleanup = bindings.api_key_cleanup
        if (
            bindings.credentials is not None
            or bindings.invitation_probe is not None
            or bindings.api_key_capture is not None
            or cleanup is None
        ):
            raise _invalid_record()
        if cleanup.observed_captured_file is not None and cleanup.observed_captured_file != cleanup.historical_captured_file:
            raise _invalid_record()
        if cleanup.terminal_outcome is ApiKeyCleanupOutcome.PUBLISHED:
            if cleanup.phase2_env_file is None or bindings.phase2_env_file != cleanup.phase2_env_file:
                raise _invalid_record()
        elif cleanup.phase2_env_file is not None or bindings.phase2_env_file is not None:
            raise _invalid_record()
        return
    if bindings.credentials is None or bindings.api_key_cleanup is not None:
        raise _invalid_record()

    codes = tuple(row.code for row in rows)
    completion_codes = tuple(code for code in codes if code in _C_CODE_SET)
    has_probe = ActionCode.BOOTSTRAP_PROBE_INVITATION in completion_codes
    if branch in {"bootstrap", "repair-completion"}:
        required_candidates = frozenset(
            identity
            for identity, action in _IDENTITY_MUTATION_ACTION.items()
            if action in completion_codes
        )
        required_currents = _COMPLETE_IDENTITY_SET - required_candidates
        create_key = ActionCode.BOOTSTRAP_CREATE_API_KEY in completion_codes
        if (
            (bindings.invitation_probe is not None) != has_probe
            or (bindings.api_key_capture is not None) != (not create_key)
            or bindings.phase2_env_file is None
        ):
            raise _invalid_record()
        _require_credential_projection(
            bindings,
            current=required_currents,
            candidate=required_candidates,
        )
        return

    if branch in _COMPLETE_BINDING_BRANCHES:
        if (
            bindings.invitation_probe is not None
            or bindings.api_key_capture is not None
            or bindings.phase2_env_file is None
        ):
            raise _invalid_record()
        _require_credential_projection(
            bindings,
            current=_COMPLETE_IDENTITY_SET,
            candidate=frozenset(),
        )
        return

    if branch in {"repair-discovery", "repair-revoke"}:
        if (
            bindings.api_key_capture is not None
            or bindings.phase2_env_file is not None
            or (
                branch == "repair-revoke"
                and bindings.invitation_probe is not None
            )
        ):
            raise _invalid_record()
        _require_credential_projection(
            bindings,
            current=_COMPLETE_IDENTITY_SET,
            candidate=frozenset(),
        )
        return

    if branch == "repair-discard":
        if (
            bindings.invitation_probe is not None
            or bindings.api_key_capture is not None
            or bindings.phase2_env_file is not None
        ):
            raise _invalid_record()
        discard = next(
            row
            for row in rows
            if row.code is ActionCode.REPAIR_DISCARD_REJECTED_CANDIDATE
        )
        target = CredentialIdentity(discard.target_id)
        target_set = frozenset({target})
        _require_credential_projection(
            bindings,
            current=target_set,
            candidate=target_set,
        )
        return

    raise _invalid_record()


class ActionRegistry:
    @staticmethod
    def definition(code: ActionCode) -> ActionDefinition:
        if type(code) is not ActionCode:
            raise _invalid_record()
        return _ACTION_DEFINITIONS[code]

    @staticmethod
    def validate(
        operation: Operation,
        profile: RuntimeProfile,
        license_mode: LicenseMode,
        bootstrap_bindings: BootstrapPlanBindings | None,
        actions: Sequence[PlannedAction],
    ) -> None:
        if (
            type(operation) is not Operation
            or type(profile) is not RuntimeProfile
            or type(license_mode) is not LicenseMode
            or type(actions) not in {tuple, list}
            or (
                bootstrap_bindings is not None
                and type(bootstrap_bindings) is not BootstrapPlanBindings
            )
        ):
            raise _invalid_record()
        rows = tuple(actions)
        if not rows or any(type(row) is not PlannedAction for row in rows):
            raise _invalid_record()
        counts = Counter(row.code for row in rows)
        if any(
            count > 1
            and not _ACTION_DEFINITIONS[code].allows_multiple_targets
            for code, count in counts.items()
        ):
            raise _invalid_record()
        ordered = tuple(
            sorted(
                rows,
                key=lambda row: (
                    _ACTION_RANK_INDEX[row.code],
                    "" if row.target_kind is None else row.target_kind.value,
                    "" if row.target_id is None else row.target_id,
                ),
            )
        )
        if rows != ordered or rows[0] != _a(ActionCode.GATEWAY_FAIL_CLOSED):
            raise _invalid_record()
        if any(operation not in _ACTION_DEFINITIONS[row.code].allowed_operations for row in rows):
            raise _invalid_record()
        _validate_targets(rows, bootstrap_bindings)

        codes = frozenset(row.code for row in rows)
        has_minio = ActionCode.READINESS_PROBE_MINIO_ROUTE in codes
        if has_minio and profile is not RuntimeProfile.ACCEPTANCE:
            raise _invalid_record()
        structural = codes - {ActionCode.READINESS_PROBE_MINIO_ROUTE}
        fail = {ActionCode.GATEWAY_FAIL_CLOSED}
        preopen = {
            ActionCode.READINESS_REQUIRE_LOOPBACK,
            ActionCode.GATEWAY_ENABLE_DUAL,
            ActionCode.READINESS_REQUIRE_DUAL,
        }
        ordinary_start = {
            ActionCode.LICENSE_VERIFY_OFFLINE
            if license_mode is LicenseMode.OFFLINE
            else ActionCode.LICENSE_DEBIT_ONLINE_START,
            ActionCode.PROCESS_CREATE_API_PERMIT,
            ActionCode.PROCESS_START_API,
        }
        branch = ""
        if operation is Operation.BOOTSTRAP:
            required = fail | ordinary_start | preopen | _C_CODE_SET | {
                ActionCode.COMPOSE_CREATE_STATE_GATEWAY,
                ActionCode.CMMS_INITIALIZE_FRESH_DATABASE,
                ActionCode.PROCESS_START_FRONTEND,
                ActionCode.READINESS_REQUIRE_API_LOOPBACK,
            }
            if not _matches_codes(structural, required, _I_BOOTSTRAP):
                raise _invalid_record()
            branch = "bootstrap"
        elif operation is Operation.START:
            if ActionCode.COMPOSE_START_STATE_GATEWAY in structural:
                required = fail | ordinary_start | preopen | {
                    ActionCode.COMPOSE_START_STATE_GATEWAY,
                    ActionCode.PROCESS_START_FRONTEND,
                }
                if not _matches_codes(structural, required):
                    raise _invalid_record()
                branch = "start-stopped"
            else:
                if not _matches_codes(
                    structural,
                    fail | preopen,
                    frozenset({ActionCode.PROCESS_START_FRONTEND}),
                ):
                    raise _invalid_record()
                branch = "start-active"
            if has_minio:
                raise _invalid_record()
        elif operation is Operation.RESTART_API:
            required = fail | ordinary_start | preopen | {
                ActionCode.PROCESS_STOP_API,
                ActionCode.BUILD_API,
            }
            if has_minio or not _matches_codes(
                structural,
                required,
                frozenset({ActionCode.LICENSE_STOP_GUARD}),
            ):
                raise _invalid_record()
            branch = "restart-api"
        elif operation is Operation.RESTART_FRONTEND:
            required = fail | preopen | {
                ActionCode.PROCESS_STOP_FRONTEND,
                ActionCode.PROCESS_START_FRONTEND,
            }
            if has_minio or structural != required:
                raise _invalid_record()
            branch = "restart-frontend"
        elif operation is Operation.STOP:
            required = fail | {
                ActionCode.PROCESS_STOP_FRONTEND,
                ActionCode.PROCESS_STOP_API,
                ActionCode.COMPOSE_STOP_STATE_GATEWAY,
            }
            if has_minio or not _matches_codes(
                structural,
                required,
                frozenset({ActionCode.LICENSE_STOP_GUARD}),
            ):
                raise _invalid_record()
            branch = "stop"
        elif operation is Operation.SWITCH_LICENSE:
            required = fail | ordinary_start | preopen | {
                ActionCode.PROCESS_STOP_API,
                ActionCode.LICENSE_SWITCH_MODE,
            }
            if has_minio or not _matches_codes(
                structural,
                required,
                frozenset(
                    {ActionCode.LICENSE_STOP_GUARD, ActionCode.PROCESS_START_FRONTEND}
                ),
            ):
                raise _invalid_record()
            branch = "switch-license"
        elif operation is Operation.REPAIR:
            cleanup = ActionCode.REPAIR_FINALIZE_API_KEY_CAPTURE_CLEANUP in structural
            recovery = ActionCode.LICENSE_RECOVER_UNKNOWN_BUDGET in structural
            discovery = ActionCode.REPAIR_CAPTURE_BOOTSTRAP_DISCOVERY in structural
            revoke = ActionCode.REPAIR_REVOKE_UNCAPTURED_API_KEY in structural
            discard = ActionCode.REPAIR_DISCARD_REJECTED_CANDIDATE in structural
            sentinels = sum((cleanup, recovery, discovery, revoke, discard))
            if sentinels > 1:
                raise _invalid_record()
            if cleanup:
                if has_minio or structural != fail | {ActionCode.REPAIR_FINALIZE_API_KEY_CAPTURE_CLEANUP}:
                    raise _invalid_record()
                branch = "repair-cleanup"
            elif recovery:
                required = fail | preopen | {
                    ActionCode.LICENSE_RECOVER_UNKNOWN_BUDGET,
                    ActionCode.PROCESS_CREATE_API_PERMIT,
                    ActionCode.PROCESS_START_API,
                    ActionCode.PROCESS_START_FRONTEND,
                }
                if license_mode is not LicenseMode.ONLINE or has_minio or not _matches_codes(structural, required, _I_REPAIR_FULL):
                    raise _invalid_record()
                branch = "repair-budget"
            elif discovery or revoke or discard:
                sentinel = (
                    ActionCode.REPAIR_CAPTURE_BOOTSTRAP_DISCOVERY
                    if discovery
                    else ActionCode.REPAIR_REVOKE_UNCAPTURED_API_KEY
                    if revoke
                    else ActionCode.REPAIR_DISCARD_REJECTED_CANDIDATE
                )
                required = fail | ordinary_start | {
                    ActionCode.READINESS_REQUIRE_API_LOOPBACK,
                    sentinel,
                    ActionCode.REPAIR_STOP_LOOPBACK_RUNTIME,
                }
                if has_minio or not _matches_codes(structural, required, _I_REPAIR_API):
                    raise _invalid_record()
                branch = (
                    "repair-discovery" if discovery else "repair-revoke" if revoke else "repair-discard"
                )
            else:
                common = fail | ordinary_start | preopen | {
                    ActionCode.PROCESS_START_FRONTEND,
                    ActionCode.READINESS_REQUIRE_API_LOOPBACK,
                }
                completion = tuple(row.code for row in rows if row.code in _C_CODE_SET)
                if completion:
                    if not _legal_completion_suffix(completion) or not _matches_codes(structural, common | set(completion), _I_REPAIR_FULL):
                        raise _invalid_record()
                    branch = "repair-completion"
                else:
                    if not _matches_codes(structural, common, _I_REPAIR_FULL):
                        raise _invalid_record()
                    branch = "repair-readiness"
                if has_minio and profile is not RuntimeProfile.ACCEPTANCE:
                    raise _invalid_record()
        if not branch:
            raise _invalid_record()
        _validate_bootstrap_bindings(
            bootstrap_bindings,
            branch=branch,
            rows=rows,
        )


@dataclass(frozen=True)
class DeploymentPlan:
    plan_sha256: str
    plan_nonce: str
    created_at: datetime
    expires_at: datetime
    snapshot: DeploymentSnapshot
    operation: Operation
    profile: RuntimeProfile
    license_mode: LicenseMode
    bootstrap_bindings: BootstrapPlanBindings | None
    actions: tuple[PlannedAction, ...]

    def __post_init__(self) -> None:
        _require_hex64(self.plan_sha256)
        _require_hex32(self.plan_nonce)
        if self.expires_at - self.created_at != timedelta(minutes=30):
            raise _invalid_record()
        if type(self.snapshot) is not DeploymentSnapshot:
            raise _invalid_record()
        if type(self.operation) is not Operation or type(self.profile) is not RuntimeProfile or type(self.license_mode) is not LicenseMode:
            raise _invalid_record()
        object.__setattr__(self, "actions", tuple(self.actions))
        ActionRegistry.validate(
            self.operation,
            self.profile,
            self.license_mode,
            self.bootstrap_bindings,
            self.actions,
        )
        if self.profile is RuntimeProfile.ACCEPTANCE:
            source = self.snapshot.source
            if (
                source.root_status is not SourceStatus.CLEAN
                or source.cmms_status is not SourceStatus.CLEAN
                or source.cmms_gitlink != source.cmms_head
            ):
                raise _invalid_record()
        expected = hashlib.sha256(canonical_json_bytes(self._unsigned_mapping())).hexdigest()
        if self.plan_sha256 != expected:
            raise _invalid_record()

    @classmethod
    def create(
        cls,
        *,
        snapshot: DeploymentSnapshot,
        operation: Operation,
        profile: RuntimeProfile,
        license_mode: LicenseMode,
        bootstrap_bindings: BootstrapPlanBindings | None,
        actions: Sequence[PlannedAction],
        now: datetime,
        plan_nonce: str,
    ) -> DeploymentPlan:
        if (
            bootstrap_bindings is not None
            and type(bootstrap_bindings) is not BootstrapPlanBindings
        ):
            raise _invalid_record()
        nonce = _require_hex32(plan_nonce)
        created = _parse_utc(_format_utc(now))
        expires = created + timedelta(minutes=30)
        values = {
            "plan_nonce": nonce,
            "created_at": created,
            "expires_at": expires,
            "snapshot": snapshot,
            "operation": operation,
            "profile": profile,
            "license_mode": license_mode,
            "bootstrap_bindings": bootstrap_bindings,
            "actions": tuple(actions),
        }
        unsigned = cls._build_unsigned(**values)
        digest = hashlib.sha256(canonical_json_bytes(unsigned)).hexdigest()
        return cls(plan_sha256=digest, **values)

    @staticmethod
    def _build_unsigned(
        *,
        plan_nonce: str,
        created_at: datetime,
        expires_at: datetime,
        snapshot: DeploymentSnapshot,
        operation: Operation,
        profile: RuntimeProfile,
        license_mode: LicenseMode,
        bootstrap_bindings: BootstrapPlanBindings | None,
        actions: tuple[PlannedAction, ...],
    ) -> dict[str, JsonValue]:
        return {
            "schema_version": 1,
            "record_type": "cmms-deployment-plan",
            "plan_nonce": plan_nonce,
            "created_at": _format_utc(created_at),
            "expires_at": _format_utc(expires_at),
            "snapshot": snapshot.to_mapping(),
            "operation": operation.value,
            "profile": profile.value,
            "license_mode": license_mode.value,
            "bootstrap_bindings": bootstrap_bindings.to_mapping() if bootstrap_bindings else None,
            "actions": [row.to_mapping() for row in actions],
        }

    def _unsigned_mapping(self) -> dict[str, JsonValue]:
        return self._build_unsigned(
            plan_nonce=self.plan_nonce,
            created_at=self.created_at,
            expires_at=self.expires_at,
            snapshot=self.snapshot,
            operation=self.operation,
            profile=self.profile,
            license_mode=self.license_mode,
            bootstrap_bindings=self.bootstrap_bindings,
            actions=self.actions,
        )

    def to_mapping(self) -> dict[str, JsonValue]:
        unsigned = self._unsigned_mapping()
        return {
            "schema_version": unsigned["schema_version"],
            "record_type": unsigned["record_type"],
            "plan_sha256": self.plan_sha256,
            **{key: value for key, value in unsigned.items() if key not in {"schema_version", "record_type"}},
        }

    @classmethod
    def from_mapping(cls, value: object) -> DeploymentPlan:
        if type(value) is not dict:
            raise _invalid_record()
        keys = (
            "schema_version", "record_type", "plan_sha256", "plan_nonce",
            "created_at", "expires_at", "snapshot", "operation", "profile",
            "license_mode", "bootstrap_bindings", "actions",
        )
        _require_exact_keys(value, keys)
        if (
            type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or value["record_type"] != "cmms-deployment-plan"
            or type(value["actions"]) is not list
        ):
            raise _invalid_record()
        return cls(
            plan_sha256=_require_hex64(value["plan_sha256"]),
            plan_nonce=_require_hex32(value["plan_nonce"]),
            created_at=_parse_utc(value["created_at"]),
            expires_at=_parse_utc(value["expires_at"]),
            snapshot=DeploymentSnapshot.from_mapping(value["snapshot"]),
            operation=_enum(Operation, value["operation"]),
            profile=_enum(RuntimeProfile, value["profile"]),
            license_mode=_enum(LicenseMode, value["license_mode"]),
            bootstrap_bindings=(None if value["bootstrap_bindings"] is None else BootstrapPlanBindings.from_mapping(value["bootstrap_bindings"])),
            actions=tuple(PlannedAction.from_mapping(row) for row in value["actions"]),
        )


def _plan_policy(plans_dir: Path, paths: Sequence[Path]) -> RuntimePathPolicy:
    return RuntimePathPolicy.for_test(plans_dir, allowed_files=set(paths))


def write_plan(plan: DeploymentPlan, plans_dir: Path) -> tuple[Path, str]:
    if type(plan) is not DeploymentPlan:
        raise _invalid_record()
    path = Path(plans_dir) / f"{plan.plan_sha256}.json"
    atomic_write_private(
        path,
        canonical_json_bytes(plan.to_mapping()),
        replace=False,
        policy=_plan_policy(Path(plans_dir), (path,)),
    )
    return path, plan.plan_sha256


def _load_plan_file(path: Path, plans_dir: Path) -> DeploymentPlan:
    raw_dir = os.fspath(plans_dir)
    raw_path = os.fspath(path)
    canonical_dir = Path(os.path.abspath(raw_dir))
    canonical_path = Path(os.path.abspath(raw_path))
    if raw_dir != os.fspath(canonical_dir) or raw_path != os.fspath(canonical_path):
        raise _invalid_record()
    if canonical_path.parent != canonical_dir or not canonical_path.name.endswith(".json"):
        raise _invalid_record()
    data = read_secure_bytes(
        canonical_path,
        max_bytes=_PLAN_MAX_BYTES,
        policy=_plan_policy(canonical_dir, (canonical_path,)),
    )
    value = strict_canonical_json_loads(data, max_bytes=_PLAN_MAX_BYTES, max_depth=16)
    plan = DeploymentPlan.from_mapping(value)
    if canonical_path.name != f"{plan.plan_sha256}.json":
        raise _invalid_record()
    return plan


@dataclass(frozen=True)
class PlanApplicationRecord:
    application_id: str
    application_generation: int
    plan_sha256: str
    state: PlanApplicationState
    attempted_at: datetime
    claimed_at: datetime | None
    terminal_at: datetime | None
    safe_result_codes: tuple[ApplicationResultCode, ...]

    def __post_init__(self) -> None:
        _require_hex32(self.application_id)
        _require_hex64(self.plan_sha256)
        _require_int(self.application_generation, minimum=1)
        if type(self.state) is not PlanApplicationState:
            raise _invalid_record()
        object.__setattr__(self, "safe_result_codes", tuple(self.safe_result_codes))
        if any(type(code) is not ApplicationResultCode for code in self.safe_result_codes) or len(set(self.safe_result_codes)) != len(self.safe_result_codes) or len(self.safe_result_codes) > 32:
            raise _invalid_record()
        _parse_utc(_format_utc(self.attempted_at))
        if self.claimed_at is not None:
            _parse_utc(_format_utc(self.claimed_at))
        if self.terminal_at is not None:
            _parse_utc(_format_utc(self.terminal_at))
        primary = {
            PlanApplicationState.CONTENDED: ApplicationResultCode.APPLY_CONTENDED,
            PlanApplicationState.REJECTED: ApplicationResultCode.APPLY_REJECTED,
            PlanApplicationState.SUCCEEDED: ApplicationResultCode.APPLY_SUCCEEDED,
            PlanApplicationState.FAILED: ApplicationResultCode.APPLY_FAILED,
        }
        valid = {
            PlanApplicationState.ATTEMPTED: (1, None, None, ()),
            PlanApplicationState.CONTENDED: (2, None, "terminal", (primary[PlanApplicationState.CONTENDED],)),
            PlanApplicationState.REJECTED: (2, None, "terminal", (primary[PlanApplicationState.REJECTED],)),
            PlanApplicationState.IN_PROGRESS: (2, "claimed", None, ()),
            PlanApplicationState.SUCCEEDED: (3, "claimed", "terminal", (primary[PlanApplicationState.SUCCEEDED],)),
            PlanApplicationState.FAILED: (3, "claimed", "terminal", (primary[PlanApplicationState.FAILED],)),
        }
        generation, claimed_marker, terminal_marker, codes = valid[self.state]
        if self.application_generation != generation or (self.claimed_at is not None) != (claimed_marker is not None) or (self.terminal_at is not None) != (terminal_marker is not None) or self.safe_result_codes != codes:
            raise _invalid_record()
        if self.claimed_at is not None and self.claimed_at <= self.attempted_at:
            raise _invalid_record()
        latest = self.claimed_at or self.attempted_at
        if self.terminal_at is not None and self.terminal_at <= latest:
            raise _invalid_record()

    @classmethod
    def attempted(
        cls, plan_sha256: str, now: datetime, application_id: str | None = None
    ) -> PlanApplicationRecord:
        return cls(
            application_id=secrets.token_hex(16) if application_id is None else application_id,
            application_generation=1,
            plan_sha256=plan_sha256,
            state=PlanApplicationState.ATTEMPTED,
            attempted_at=_parse_utc(_format_utc(now)),
            claimed_at=None,
            terminal_at=None,
            safe_result_codes=(),
        )

    def transition(
        self,
        state: PlanApplicationState,
        now: datetime,
        secondary_result_codes: Sequence[ApplicationResultCode] = (),
    ) -> PlanApplicationRecord:
        if secondary_result_codes or type(state) is not PlanApplicationState:
            raise _invalid_record()
        timestamp = _parse_utc(_format_utc(now))
        if self.state is PlanApplicationState.ATTEMPTED and state in {
            PlanApplicationState.CONTENDED,
            PlanApplicationState.REJECTED,
        }:
            code = ApplicationResultCode.APPLY_CONTENDED if state is PlanApplicationState.CONTENDED else ApplicationResultCode.APPLY_REJECTED
            return replace(self, application_generation=2, state=state, terminal_at=timestamp, safe_result_codes=(code,))
        if self.state is PlanApplicationState.ATTEMPTED and state is PlanApplicationState.IN_PROGRESS:
            return replace(self, application_generation=2, state=state, claimed_at=timestamp)
        if self.state is PlanApplicationState.IN_PROGRESS and state in {PlanApplicationState.SUCCEEDED, PlanApplicationState.FAILED}:
            code = ApplicationResultCode.APPLY_SUCCEEDED if state is PlanApplicationState.SUCCEEDED else ApplicationResultCode.APPLY_FAILED
            return replace(self, application_generation=3, state=state, terminal_at=timestamp, safe_result_codes=(code,))
        raise _invalid_record()

    def to_mapping(self) -> dict[str, JsonValue]:
        return {
            "schema_version": 1,
            "record_type": "cmms-plan-application",
            "application_id": self.application_id,
            "application_generation": self.application_generation,
            "plan_sha256": self.plan_sha256,
            "state": self.state.value,
            "attempted_at": _format_utc(self.attempted_at),
            "claimed_at": None if self.claimed_at is None else _format_utc(self.claimed_at),
            "terminal_at": None if self.terminal_at is None else _format_utc(self.terminal_at),
            "safe_result_codes": [code.value for code in self.safe_result_codes],
        }

    @classmethod
    def from_mapping(cls, value: object) -> PlanApplicationRecord:
        if type(value) is not dict:
            raise _invalid_record()
        _require_exact_keys(value, ("schema_version", "record_type", "application_id", "application_generation", "plan_sha256", "state", "attempted_at", "claimed_at", "terminal_at", "safe_result_codes"))
        if (
            type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or value["record_type"] != "cmms-plan-application"
            or type(value["safe_result_codes"]) is not list
        ):
            raise _invalid_record()
        return cls(
            application_id=_require_hex32(value["application_id"]),
            application_generation=_require_int(value["application_generation"], minimum=1),
            plan_sha256=_require_hex64(value["plan_sha256"]),
            state=_enum(PlanApplicationState, value["state"]),
            attempted_at=_parse_utc(value["attempted_at"]),
            claimed_at=None if value["claimed_at"] is None else _parse_utc(value["claimed_at"]),
            terminal_at=None if value["terminal_at"] is None else _parse_utc(value["terminal_at"]),
            safe_result_codes=tuple(_enum(ApplicationResultCode, code) for code in value["safe_result_codes"]),
        )

    @classmethod
    def load(cls, path: Path) -> PlanApplicationRecord:
        path = Path(path)
        data = read_secure_bytes(
            path,
            max_bytes=_APPLICATION_MAX_BYTES,
            policy=RuntimePathPolicy.for_test(path.parent, allowed_files={path}),
        )
        value = strict_canonical_json_loads(data, max_bytes=_APPLICATION_MAX_BYTES, max_depth=8)
        record = cls.from_mapping(value)
        if path.name != f"{record.plan_sha256}.application.json":
            raise _invalid_record()
        return record


def _transition_application(
    path: Path,
    expected: PlanApplicationRecord,
    state: PlanApplicationState,
    now: datetime,
) -> PlanApplicationRecord:
    parent_fd = -1
    fd = -1
    policy = RuntimePathPolicy.for_test(path.parent, allowed_files={path})
    try:
        parent_fd, name = open_verified_parent(path, policy=policy)
        fcntl.flock(parent_fd, fcntl.LOCK_EX)
        fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=parent_fd)
        require_private_regular_file(os.fstat(fd), expected_uid=os.getuid())
        current = PlanApplicationRecord.load(path)
        if current != expected:
            raise _plan_consumed()
        updated = current.transition(state, now)
        atomic_write_private(path, canonical_json_bytes(updated.to_mapping()), replace=True, policy=policy)
        if PlanApplicationRecord.load(path) != updated:
            raise _invalid_record()
        return updated
    finally:
        if fd >= 0:
            os.close(fd)
        if parent_fd >= 0:
            os.close(parent_fd)


class PlanAttemptReservation:
    __slots__ = ("_plan", "_application", "_application_path", "_token")

    def __init__(self, token: object, plan: DeploymentPlan, application: PlanApplicationRecord, application_path: Path) -> None:
        if token is not _CAPABILITY_TOKEN:
            raise _invalid_record()
        self._token = token
        self._plan = plan
        self._application = application
        self._application_path = application_path

    @property
    def plan(self) -> DeploymentPlan:
        return self._plan

    @property
    def application(self) -> PlanApplicationRecord:
        return self._application

    @property
    def application_path(self) -> Path:
        return self._application_path

    def _replace_application(self, application: PlanApplicationRecord) -> None:
        self._application = application

    def __copy__(self) -> object:
        raise _invalid_record()

    def __deepcopy__(self, _memo: object) -> object:
        raise _invalid_record()

    def __reduce__(self) -> object:
        raise _invalid_record()


def reserve_plan_attempt(
    path: Path,
    confirmed_sha256: str,
    now: datetime,
    plans_dir: Path,
    *,
    application_id: str | None = None,
) -> PlanAttemptReservation:
    digest = _require_hex64(confirmed_sha256)
    timestamp = _parse_utc(_format_utc(now))
    plan = _load_plan_file(path, plans_dir)
    if digest != plan.plan_sha256 or not (plan.created_at <= timestamp <= plan.expires_at):
        raise _confirmation_failed()
    application = PlanApplicationRecord.attempted(plan.plan_sha256, timestamp, application_id)
    app_path = Path(plans_dir) / f"{plan.plan_sha256}.application.json"
    try:
        atomic_write_private(
            app_path,
            canonical_json_bytes(application.to_mapping()),
            replace=False,
            policy=_plan_policy(Path(plans_dir), (app_path,)),
        )
    except DeploymentError:
        raise _plan_consumed() from None
    if PlanApplicationRecord.load(app_path) != application:
        raise _invalid_record()
    return PlanAttemptReservation(_CAPABILITY_TOKEN, plan, application, app_path)


class DeploymentWriteLease:
    __slots__ = (
        "_fd",
        "_directory_fd",
        "_reservation",
        "_path",
        "_creator_pid",
        "_lock_identity",
        "_directory_identity",
        "_closed",
        "_token",
    )

    def __init__(
        self,
        token: object,
        fd: int,
        directory_fd: int,
        reservation: PlanAttemptReservation,
        path: Path,
        lock_identity: tuple[int, int],
        directory_identity: tuple[int, int],
    ) -> None:
        if token is not _CAPABILITY_TOKEN:
            raise _invalid_record()
        self._token = token
        self._fd = fd
        self._directory_fd = directory_fd
        self._reservation = reservation
        self._path = path
        self._creator_pid = os.getpid()
        self._lock_identity = lock_identity
        self._directory_identity = directory_identity
        self._closed = False

    def _require_live(self, reservation: PlanAttemptReservation | None = None) -> None:
        if (
            self._closed
            or self._fd < 0
            or self._directory_fd < 0
            or os.getpid() != self._creator_pid
        ):
            raise _confirmation_failed()
        if reservation is not None and self._reservation is not reservation:
            raise _confirmation_failed()
        fresh_parent_fd = -1
        fresh_lock_fd = -1
        try:
            directory_metadata = os.fstat(self._directory_fd)
            lock_metadata = os.fstat(self._fd)
            if (
                (directory_metadata.st_dev, directory_metadata.st_ino)
                != self._directory_identity
                or not stat.S_ISDIR(directory_metadata.st_mode)
                or directory_metadata.st_uid != os.getuid()
                or stat.S_IMODE(directory_metadata.st_mode) != 0o700
                or (lock_metadata.st_dev, lock_metadata.st_ino)
                != self._lock_identity
            ):
                raise _confirmation_failed()
            require_private_regular_file(lock_metadata, expected_uid=os.getuid())
            if lock_metadata.st_size != 0:
                raise _confirmation_failed()

            policy = RuntimePathPolicy.for_test(
                self._path.parent,
                allowed_files={self._path},
            )
            fresh_parent_fd, name = open_verified_parent(self._path, policy=policy)
            fresh_directory_metadata = os.fstat(fresh_parent_fd)
            if (
                fresh_directory_metadata.st_dev,
                fresh_directory_metadata.st_ino,
            ) != self._directory_identity:
                raise _confirmation_failed()
            fresh_lock_fd = os.open(
                name,
                os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=fresh_parent_fd,
            )
            fresh_lock_metadata = os.fstat(fresh_lock_fd)
            require_private_regular_file(
                fresh_lock_metadata,
                expected_uid=os.getuid(),
            )
            if (
                (fresh_lock_metadata.st_dev, fresh_lock_metadata.st_ino)
                != self._lock_identity
                or fresh_lock_metadata.st_size != 0
            ):
                raise _confirmation_failed()
        except DeploymentError:
            raise _confirmation_failed() from None
        except OSError:
            raise _confirmation_failed() from None
        finally:
            if fresh_lock_fd >= 0:
                os.close(fresh_lock_fd)
            if fresh_parent_fd >= 0:
                os.close(fresh_parent_fd)

    def close(self) -> None:
        if not self._closed:
            os.close(self._fd)
            os.close(self._directory_fd)
            self._fd = -1
            self._directory_fd = -1
            self._closed = True

    def __enter__(self) -> DeploymentWriteLease:
        self._require_live()
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def __copy__(self) -> object:
        raise _invalid_record()

    def __deepcopy__(self, _memo: object) -> object:
        raise _invalid_record()

    def __reduce__(self) -> object:
        raise _invalid_record()


def _after_application_time(application: PlanApplicationRecord) -> datetime:
    floor = application.claimed_at or application.terminal_at or application.attempted_at
    current = datetime.now(timezone.utc)
    return current if current > floor else floor + timedelta(microseconds=1)


def acquire_deployment_write_lease(
    lock_path: Path, reservation: PlanAttemptReservation
) -> DeploymentWriteLease:
    if type(reservation) is not PlanAttemptReservation or reservation._token is not _CAPABILITY_TOKEN:
        raise _confirmation_failed()
    raw_path = os.fspath(lock_path)
    path = Path(os.path.abspath(raw_path))
    if raw_path != os.fspath(path):
        raise _confirmation_failed()
    plans_dir = reservation.application_path.parent
    if path.parent != plans_dir.parent.parent or path.name != "cmms-development-apply.lock":
        raise _confirmation_failed()
    policy = RuntimePathPolicy.for_test(path.parent, allowed_files={path})
    parent_fd = -1
    fd = -1
    try:
        parent_fd, name = open_verified_parent(path, policy=policy)
        directory_metadata = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or directory_metadata.st_uid != os.getuid()
            or stat.S_IMODE(directory_metadata.st_mode) != 0o700
        ):
            raise _confirmation_failed()
        try:
            fcntl.flock(parent_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            updated = _transition_application(
                reservation.application_path,
                reservation.application,
                PlanApplicationState.CONTENDED,
                _after_application_time(reservation.application),
            )
            reservation._replace_application(updated)
            raise _deployment_busy() from None
        created = False
        try:
            fd = os.open(
                name,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
            created = True
        except FileExistsError:
            fd = os.open(
                name,
                os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
        if created:
            os.fchmod(fd, 0o600)
        metadata = os.fstat(fd)
        require_private_regular_file(metadata, expected_uid=os.getuid())
        if metadata.st_size != 0:
            raise _confirmation_failed()
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            updated = _transition_application(
                reservation.application_path,
                reservation.application,
                PlanApplicationState.CONTENDED,
                _after_application_time(reservation.application),
            )
            reservation._replace_application(updated)
            raise _deployment_busy() from None
        lease = DeploymentWriteLease(
            _CAPABILITY_TOKEN,
            fd,
            parent_fd,
            reservation,
            path,
            (metadata.st_dev, metadata.st_ino),
            (directory_metadata.st_dev, directory_metadata.st_ino),
        )
        fd = -1
        parent_fd = -1
        return lease
    finally:
        if fd >= 0:
            os.close(fd)
        if parent_fd >= 0:
            os.close(parent_fd)


class ConfirmedDeploymentPlan:
    __slots__ = ("_plan", "_reservation", "_lease", "_token")

    def __init__(self, token: object, plan: DeploymentPlan, reservation: PlanAttemptReservation, lease: DeploymentWriteLease) -> None:
        if token is not _CAPABILITY_TOKEN:
            raise _invalid_record()
        self._token = token
        self._plan = plan
        self._reservation = reservation
        self._lease = lease

    @property
    def plan(self) -> DeploymentPlan:
        return self._plan

    def __copy__(self) -> object:
        raise _invalid_record()

    def __deepcopy__(self, _memo: object) -> object:
        raise _invalid_record()

    def __reduce__(self) -> object:
        raise _invalid_record()


def load_confirmed_plan(
    reservation: PlanAttemptReservation,
    snapshot: DeploymentSnapshot,
    current_bootstrap_bindings: BootstrapPlanBindings | None,
    lease: DeploymentWriteLease,
) -> ConfirmedDeploymentPlan:
    if type(reservation) is not PlanAttemptReservation or type(lease) is not DeploymentWriteLease:
        raise _confirmation_failed()
    lease._require_live(reservation)
    current_application = PlanApplicationRecord.load(reservation.application_path)
    if current_application != reservation.application or current_application.state is not PlanApplicationState.ATTEMPTED:
        raise _confirmation_failed()
    plan = reservation.plan
    if snapshot != plan.snapshot or current_bootstrap_bindings != plan.bootstrap_bindings:
        updated = _transition_application(
            reservation.application_path,
            reservation.application,
            PlanApplicationState.REJECTED,
            _after_application_time(reservation.application),
        )
        reservation._replace_application(updated)
        raise _confirmation_failed()
    return ConfirmedDeploymentPlan(_CAPABILITY_TOKEN, plan, reservation, lease)


def _opaque_reduce() -> object:
    raise _invalid_record()


class _OpaqueCapability:
    __slots__ = ()

    def __copy__(self) -> object:
        raise _invalid_record()

    def __deepcopy__(self, _memo: object) -> object:
        raise _invalid_record()

    def __reduce__(self) -> object:
        raise _invalid_record()


class GatewayListenerChallenge(_OpaqueCapability):
    __slots__ = ("_authority", "_challenge_id", "_token")

    def __init__(self, token: object, authority: object, challenge_id: str) -> None:
        if token is not _CAPABILITY_TOKEN:
            raise _invalid_record()
        self._token = token
        self._authority = authority
        self._challenge_id = challenge_id


class GatewayListenerAbsentProof(_OpaqueCapability):
    __slots__ = ("_authority", "_challenge_id", "_token")

    def __init__(self, token: object, authority: object, challenge_id: str) -> None:
        if token is not _CAPABILITY_TOKEN:
            raise _invalid_record()
        self._token = token
        self._authority = authority
        self._challenge_id = challenge_id


class FailClosedEvidence(_OpaqueCapability):
    __slots__ = ("_confirmed", "_lease", "_authority", "_challenge_id", "_used", "_token")

    def __init__(self, token: object, confirmed: ConfirmedDeploymentPlan, lease: DeploymentWriteLease, authority: object, challenge_id: str) -> None:
        if token is not _CAPABILITY_TOKEN:
            raise _invalid_record()
        self._token = token
        self._confirmed = confirmed
        self._lease = lease
        self._authority = authority
        self._challenge_id = challenge_id
        self._used = False


class ClaimedApplyContext(_OpaqueCapability):
    __slots__ = ("_plan", "_application", "_lease", "_evidence", "_token")

    def __init__(self, token: object, plan: DeploymentPlan, application: PlanApplicationRecord, lease: DeploymentWriteLease, evidence: FailClosedEvidence) -> None:
        if token is not _CAPABILITY_TOKEN:
            raise _invalid_record()
        self._token = token
        self._plan = plan
        self._application = application
        self._lease = lease
        self._evidence = evidence

    @property
    def plan(self) -> DeploymentPlan:
        return self._plan

    @property
    def application(self) -> PlanApplicationRecord:
        return self._application

    def require_action(
        self,
        action: ActionCode | PlannedAction,
    ) -> PlannedAction:
        """Revalidate this live claim and return one exact planned action."""

        if self._token is not _CAPABILITY_TOKEN:
            raise _confirmation_failed()
        if type(action) is ActionCode:
            required = PlannedAction(action)
        elif type(action) is PlannedAction:
            required = action
        else:
            raise _confirmation_failed()
        confirmed = self._evidence._confirmed
        reservation = confirmed._reservation
        if (
            self._evidence._token is not _CAPABILITY_TOKEN
            or not self._evidence._used
            or confirmed._token is not _CAPABILITY_TOKEN
            or confirmed.plan is not self._plan
            or reservation.plan is not self._plan
            or self._evidence._lease is not self._lease
            or self._lease is not confirmed._lease
        ):
            raise _confirmation_failed()
        self._lease._require_live(reservation)
        current = PlanApplicationRecord.load(reservation.application_path)
        if (
            current != self._application
            or reservation.application != self._application
            or current.state is not PlanApplicationState.IN_PROGRESS
            or required not in self._plan.actions
        ):
            raise _confirmation_failed()
        return required


def _require_gateway_ipv4(value: object) -> str:
    text = _require_string(value)
    try:
        address = ipaddress.IPv4Address(text)
    except ipaddress.AddressValueError:
        raise _invalid_record() from None
    if str(address) != text or address.is_unspecified or address.is_loopback or address.is_multicast or address.is_link_local:
        raise _invalid_record()
    return text


@dataclass
class _GatewayEntry:
    state: str
    confirmed: ConfirmedDeploymentPlan
    lease: DeploymentWriteLease
    gateway_generation: str
    loopback_gateway_sha256: str
    gateway_ipv4: str


class _GatewayAuthorityState:
    def __init__(self) -> None:
        self.identity = object()
        self.lock = threading.Lock()
        self.entries: dict[str, _GatewayEntry] = {}


class _GatewayChallengeIssuer:
    def __init__(self, state: _GatewayAuthorityState) -> None:
        self._state = state

    def begin(
        self,
        confirmed: ConfirmedDeploymentPlan,
        lease: DeploymentWriteLease,
        gateway_generation: str,
        loopback_gateway_sha256: str,
        gateway_ipv4: str,
    ) -> GatewayListenerChallenge:
        if type(confirmed) is not ConfirmedDeploymentPlan or confirmed._token is not _CAPABILITY_TOKEN or confirmed._lease is not lease:
            raise _confirmation_failed()
        lease._require_live(confirmed._reservation)
        if confirmed.plan.actions[0] != _a(ActionCode.GATEWAY_FAIL_CLOSED) or confirmed._reservation.application.state is not PlanApplicationState.ATTEMPTED:
            raise _confirmation_failed()
        generation = _require_hex64(gateway_generation)
        digest = _require_hex64(loopback_gateway_sha256)
        address = _require_gateway_ipv4(gateway_ipv4)
        challenge_id = secrets.token_hex(16)
        with self._state.lock:
            self._state.entries[challenge_id] = _GatewayEntry(
                "PENDING", confirmed, lease, generation, digest, address
            )
        return GatewayListenerChallenge(_CAPABILITY_TOKEN, self._state.identity, challenge_id)


class _GatewayListenerProofMint:
    def __init__(self, state: _GatewayAuthorityState) -> None:
        self._state = state

    def mint_absent(
        self,
        challenge: GatewayListenerChallenge,
        *,
        loaded_generation: str,
        loaded_sha256: str,
        checked_ipv4: str,
        checked_port: int,
        listener_count: int,
    ) -> GatewayListenerAbsentProof:
        if type(challenge) is not GatewayListenerChallenge or challenge._token is not _CAPABILITY_TOKEN or challenge._authority is not self._state.identity:
            raise _confirmation_failed()
        with self._state.lock:
            entry = self._state.entries.get(challenge._challenge_id)
            if entry is None or entry.state != "PENDING" or loaded_generation != entry.gateway_generation or loaded_sha256 != entry.loopback_gateway_sha256 or checked_ipv4 != entry.gateway_ipv4 or type(checked_port) is not int or checked_port != 3000 or type(listener_count) is not int or listener_count != 0:
                raise _confirmation_failed()
            entry.lease._require_live(entry.confirmed._reservation)
            entry.state = "PROVED"
        return GatewayListenerAbsentProof(
            _CAPABILITY_TOKEN, self._state.identity, challenge._challenge_id
        )


class _GatewayEvidenceIssuer:
    def __init__(self, state: _GatewayAuthorityState) -> None:
        self._state = state

    def issue(
        self,
        confirmed: ConfirmedDeploymentPlan,
        lease: DeploymentWriteLease,
        proof: GatewayListenerAbsentProof,
    ) -> FailClosedEvidence:
        if type(proof) is not GatewayListenerAbsentProof or proof._token is not _CAPABILITY_TOKEN or proof._authority is not self._state.identity:
            raise _confirmation_failed()
        with self._state.lock:
            entry = self._state.entries.get(proof._challenge_id)
            if entry is None or entry.state != "PROVED" or entry.confirmed is not confirmed or entry.lease is not lease:
                raise _confirmation_failed()
            lease._require_live(confirmed._reservation)
            entry.state = "CONSUMED"
        return FailClosedEvidence(
            _CAPABILITY_TOKEN,
            confirmed,
            lease,
            self._state.identity,
            proof._challenge_id,
        )


@dataclass(frozen=True)
class _GatewayFailClosedAuthorityParts:
    challenge_issuer: _GatewayChallengeIssuer
    listener_proof_mint: _GatewayListenerProofMint
    evidence_issuer: _GatewayEvidenceIssuer


def _create_gateway_fail_closed_authority_parts() -> _GatewayFailClosedAuthorityParts:
    state = _GatewayAuthorityState()
    return _GatewayFailClosedAuthorityParts(
        _GatewayChallengeIssuer(state),
        _GatewayListenerProofMint(state),
        _GatewayEvidenceIssuer(state),
    )


def claim_plan_application(
    confirmed: ConfirmedDeploymentPlan,
    plans_dir: Path,
    lease: DeploymentWriteLease,
    evidence: FailClosedEvidence,
) -> ClaimedApplyContext:
    if (
        type(confirmed) is not ConfirmedDeploymentPlan
        or confirmed._token is not _CAPABILITY_TOKEN
        or type(evidence) is not FailClosedEvidence
        or evidence._token is not _CAPABILITY_TOKEN
        or evidence._used
        or evidence._confirmed is not confirmed
        or evidence._lease is not lease
        or confirmed._lease is not lease
        or confirmed._reservation.application_path.parent != Path(os.path.abspath(plans_dir))
    ):
        raise _confirmation_failed()
    lease._require_live(confirmed._reservation)
    updated = _transition_application(
        confirmed._reservation.application_path,
        confirmed._reservation.application,
        PlanApplicationState.IN_PROGRESS,
        _after_application_time(confirmed._reservation.application),
    )
    confirmed._reservation._replace_application(updated)
    evidence._used = True
    return ClaimedApplyContext(
        _CAPABILITY_TOKEN, confirmed.plan, updated, lease, evidence
    )


_STATE_FIELDS = (
    "schema_version", "record_type", "generation", "root_sha",
    "root_dirty_fingerprint", "root_status", "cmms_gitlink", "cmms_head",
    "cmms_dirty_fingerprint", "cmms_status", "config_sha256",
    "toolchain_manifest_sha256", "sensitive_manifest_sha256",
    "api_artifact_sha256", "frontend_lock_sha256",
    "controller_entrypoint_sha256", "controller_package_sha256",
    "unit_generation", "compose_project", "postgres_volume_name",
    "postgres_volume_identity", "minio_volume_name", "minio_volume_identity",
    "api_main_pid", "api_process_start_ticks", "frontend_main_pid",
    "frontend_process_start_ticks", "docker_gateway_ipv4",
    "loopback_gateway_sha256", "gateway_generation", "gateway_mode",
    "license_mode", "license_guard_generation", "online_budget_ledger_sha256",
    "latest_budget_debit_id", "latest_budget_sequence", "latest_budget_local_date",
    "bootstrap_receipt_sha256", "bootstrap_state", "last_plan_sha256",
    "last_operation", "last_transition_code", "updated_at",
)


def _optional_positive(value: object) -> int | None:
    return None if value is None else _require_int(value, minimum=1)


def _optional_hex(value: object, function: Any) -> str | None:
    return None if value is None else function(value)


def _canonical_date(value: object) -> str:
    text = _require_string(value)
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise _invalid_record() from None
    if parsed.isoformat() != text:
        raise _invalid_record()
    return text


_STATE_RECORD_TOKEN = object()


@dataclass(frozen=True, init=False)
class StateRecord:
    _values: Mapping[str, JsonValue] = field(repr=False)

    def __init__(
        self,
        value: Mapping[str, JsonValue],
        *,
        _token: object | None = None,
    ) -> None:
        if _token is _STATE_RECORD_TOKEN:
            parsed = dict(value)
        else:
            parsed = StateRecord.from_mapping(value).to_mapping()
        object.__setattr__(self, "_values", MappingProxyType(parsed))

    def _string(self, name: str) -> str:
        return _require_string(self._values[name])

    def _optional_string(self, name: str) -> str | None:
        value = self._values[name]
        return None if value is None else _require_string(value)

    def _integer(self, name: str) -> int:
        return _require_int(self._values[name])

    def _optional_integer(self, name: str) -> int | None:
        value = self._values[name]
        return None if value is None else _require_int(value, minimum=1)

    @property
    def schema_version(self) -> int:
        return self._integer("schema_version")

    @property
    def record_type(self) -> str:
        return self._string("record_type")

    @property
    def generation(self) -> int:
        return self._values["generation"]  # type: ignore[return-value]

    @property
    def root_sha(self) -> str:
        return self._string("root_sha")

    @property
    def root_dirty_fingerprint(self) -> str:
        return self._string("root_dirty_fingerprint")

    @property
    def root_status(self) -> SourceStatus:
        return SourceStatus(self._string("root_status"))

    @property
    def cmms_gitlink(self) -> str:
        return self._string("cmms_gitlink")

    @property
    def cmms_head(self) -> str:
        return self._string("cmms_head")

    @property
    def cmms_dirty_fingerprint(self) -> str:
        return self._string("cmms_dirty_fingerprint")

    @property
    def cmms_status(self) -> SourceStatus:
        return SourceStatus(self._string("cmms_status"))

    @property
    def config_sha256(self) -> str:
        return self._string("config_sha256")

    @property
    def toolchain_manifest_sha256(self) -> str:
        return self._string("toolchain_manifest_sha256")

    @property
    def sensitive_manifest_sha256(self) -> str:
        return self._string("sensitive_manifest_sha256")

    @property
    def api_artifact_sha256(self) -> str:
        return self._string("api_artifact_sha256")

    @property
    def frontend_lock_sha256(self) -> str:
        return self._string("frontend_lock_sha256")

    @property
    def controller_entrypoint_sha256(self) -> str:
        return self._string("controller_entrypoint_sha256")

    @property
    def controller_package_sha256(self) -> str:
        return self._string("controller_package_sha256")

    @property
    def unit_generation(self) -> str:
        return self._string("unit_generation")

    @property
    def compose_project(self) -> str:
        return self._string("compose_project")

    @property
    def postgres_volume_name(self) -> str:
        return self._string("postgres_volume_name")

    @property
    def postgres_volume_identity(self) -> str:
        return self._string("postgres_volume_identity")

    @property
    def minio_volume_name(self) -> str:
        return self._string("minio_volume_name")

    @property
    def minio_volume_identity(self) -> str:
        return self._string("minio_volume_identity")

    @property
    def api_main_pid(self) -> int | None:
        return self._optional_integer("api_main_pid")

    @property
    def api_process_start_ticks(self) -> int | None:
        return self._optional_integer("api_process_start_ticks")

    @property
    def frontend_main_pid(self) -> int | None:
        return self._optional_integer("frontend_main_pid")

    @property
    def frontend_process_start_ticks(self) -> int | None:
        return self._optional_integer("frontend_process_start_ticks")

    @property
    def docker_gateway_ipv4(self) -> str | None:
        return self._optional_string("docker_gateway_ipv4")

    @property
    def loopback_gateway_sha256(self) -> str | None:
        return self._optional_string("loopback_gateway_sha256")

    @property
    def gateway_generation(self) -> str | None:
        return self._optional_string("gateway_generation")

    @property
    def gateway_mode(self) -> GatewayMode:
        return GatewayMode(self._string("gateway_mode"))

    @property
    def license_mode(self) -> LicenseMode:
        return LicenseMode(self._string("license_mode"))

    @property
    def license_guard_generation(self) -> str | None:
        return self._optional_string("license_guard_generation")

    @property
    def updated_at(self) -> datetime:
        return _parse_utc(self._values["updated_at"])

    @property
    def online_budget_ledger_sha256(self) -> str | None:
        return self._values["online_budget_ledger_sha256"]  # type: ignore[return-value]

    @property
    def latest_budget_debit_id(self) -> str | None:
        return self._optional_string("latest_budget_debit_id")

    @property
    def latest_budget_sequence(self) -> int | None:
        return self._optional_integer("latest_budget_sequence")

    @property
    def latest_budget_local_date(self) -> str | None:
        return self._optional_string("latest_budget_local_date")

    @property
    def bootstrap_receipt_sha256(self) -> str | None:
        return self._optional_string("bootstrap_receipt_sha256")

    @property
    def bootstrap_state(self) -> BootstrapState:
        return BootstrapState(self._string("bootstrap_state"))

    @property
    def last_plan_sha256(self) -> str:
        return self._string("last_plan_sha256")

    @property
    def last_operation(self) -> Operation:
        return Operation(self._string("last_operation"))

    @property
    def last_transition_code(self) -> str:
        return self._string("last_transition_code")

    def to_mapping(self) -> dict[str, JsonValue]:
        return dict(self._values)

    @classmethod
    def from_mapping(cls, value: object) -> StateRecord:
        if type(value) is not dict:
            raise _invalid_record()
        _require_exact_keys(value, _STATE_FIELDS)
        if (
            type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or value["record_type"] != "cmms-development-state"
        ):
            raise _invalid_record()
        parsed: dict[str, JsonValue] = dict(value)  # defensive copy
        parsed["generation"] = _require_int(value["generation"], minimum=1)
        for key in ("root_sha", "cmms_gitlink", "cmms_head"):
            parsed[key] = _require_hex40(value[key])
        for key in (
            "root_dirty_fingerprint", "cmms_dirty_fingerprint", "config_sha256",
            "toolchain_manifest_sha256", "sensitive_manifest_sha256",
            "api_artifact_sha256", "frontend_lock_sha256",
            "controller_entrypoint_sha256", "controller_package_sha256",
            "unit_generation", "postgres_volume_identity", "minio_volume_identity",
            "last_plan_sha256",
        ):
            parsed[key] = _require_hex64(value[key])
        parsed["root_status"] = _enum(SourceStatus, value["root_status"]).value
        parsed["cmms_status"] = _enum(SourceStatus, value["cmms_status"]).value
        if value["compose_project"] != "ifactory-cmms-dev" or value["postgres_volume_name"] != "ifactory-cmms-dev_postgres_data" or value["minio_volume_name"] != "ifactory-cmms-dev_minio_data":
            raise _invalid_record()
        for key in ("api_main_pid", "api_process_start_ticks", "frontend_main_pid", "frontend_process_start_ticks"):
            parsed[key] = _optional_positive(value[key])
        address = value["docker_gateway_ipv4"]
        parsed["docker_gateway_ipv4"] = None if address is None else _require_gateway_ipv4(address)
        for key in ("loopback_gateway_sha256", "gateway_generation", "license_guard_generation", "online_budget_ledger_sha256", "bootstrap_receipt_sha256"):
            parsed[key] = _optional_hex(value[key], _require_hex64)
        parsed["gateway_mode"] = _enum(GatewayMode, value["gateway_mode"]).value
        parsed["license_mode"] = _enum(LicenseMode, value["license_mode"]).value
        parsed["latest_budget_debit_id"] = _optional_hex(value["latest_budget_debit_id"], _require_hex32)
        parsed["latest_budget_sequence"] = _optional_positive(value["latest_budget_sequence"])
        parsed["latest_budget_local_date"] = None if value["latest_budget_local_date"] is None else _canonical_date(value["latest_budget_local_date"])
        parsed["bootstrap_state"] = _enum(BootstrapState, value["bootstrap_state"]).value
        parsed["last_operation"] = _enum(Operation, value["last_operation"]).value
        parsed["last_transition_code"] = _require_pattern(value["last_transition_code"], _SAFE_CODE)
        parsed["updated_at"] = _format_utc(_parse_utc(value["updated_at"]))

        api_pair = (parsed["api_main_pid"], parsed["api_process_start_ticks"])
        frontend_pair = (parsed["frontend_main_pid"], parsed["frontend_process_start_ticks"])
        if (api_pair[0] is None) != (api_pair[1] is None) or (frontend_pair[0] is None) != (frontend_pair[1] is None):
            raise _invalid_record()
        gateway = (
            parsed["docker_gateway_ipv4"], parsed["loopback_gateway_sha256"], parsed["gateway_generation"]
        )
        mode = GatewayMode(parsed["gateway_mode"])
        if mode is GatewayMode.STOPPED and any(item is not None for item in gateway):
            raise _invalid_record()
        if mode in {GatewayMode.LOOPBACK, GatewayMode.DUAL} and any(item is None for item in gateway):
            raise _invalid_record()
        if mode is GatewayMode.DUAL and (api_pair[0] is None or frontend_pair[0] is None):
            raise _invalid_record()
        if LicenseMode(parsed["license_mode"]) is LicenseMode.ONLINE:
            if parsed["license_guard_generation"] is not None:
                raise _invalid_record()
        elif (parsed["license_guard_generation"] is not None) != (api_pair[0] is not None):
            raise _invalid_record()
        latest = (
            parsed["latest_budget_debit_id"], parsed["latest_budget_sequence"], parsed["latest_budget_local_date"]
        )
        if not (all(item is None for item in latest) or all(item is not None for item in latest)):
            raise _invalid_record()
        if parsed["online_budget_ledger_sha256"] is None and any(item is not None for item in latest):
            raise _invalid_record()
        if parsed["bootstrap_receipt_sha256"] is None and BootstrapState(parsed["bootstrap_state"]) is not BootstrapState.UNINITIALIZED:
            raise _invalid_record()
        _validate_json(parsed, max_depth=8)
        return cls(parsed, _token=_STATE_RECORD_TOKEN)

    @classmethod
    def from_bytes(cls, data: bytes) -> StateRecord:
        return cls.from_mapping(
            strict_canonical_json_loads(data, max_bytes=_PLAN_MAX_BYTES, max_depth=8)
        )

    def require_successor(self, previous: StateRecord | None) -> None:
        if previous is None:
            if self.generation != 1:
                raise _invalid_record()
            return
        if type(previous) is not StateRecord or self.generation != previous.generation + 1 or self.updated_at <= previous.updated_at:
            raise _invalid_record()
        if previous.online_budget_ledger_sha256 is not None and self.online_budget_ledger_sha256 is None:
            raise _invalid_record()


@dataclass(frozen=True)
class FixedRecordSchema:
    record_name: str
    required_fields: tuple[str, ...]
    fixed_values: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        if not self.record_name or len(set(self.required_fields)) != len(self.required_fields) or not set(self.fixed_values).issubset(self.required_fields):
            raise _invalid_record()
        object.__setattr__(self, "required_fields", tuple(self.required_fields))
        object.__setattr__(self, "fixed_values", MappingProxyType(dict(self.fixed_values)))


def require_exact_record_fields(
    value: Mapping[str, JsonValue], schema: FixedRecordSchema
) -> None:
    if type(value) is not dict or type(schema) is not FixedRecordSchema:
        raise _invalid_record()
    _require_exact_keys(value, schema.required_fields)
    for key, expected in schema.fixed_values.items():
        if value[key] != expected or type(value[key]) is not type(expected):
            raise _invalid_record()
    _validate_json(value, max_depth=16)


START_PERMIT_SCHEMA = FixedRecordSchema(
    "StartPermit",
    (
        "schema_version", "nonce", "plan_sha256", "created_at", "expires_at",
        "uid", "root_sha", "cmms_source_fingerprint", "api_artifact_sha256",
        "controller_entrypoint_sha256", "controller_package_sha256",
        "unit_generation", "loopback_gateway_sha256", "docker_gateway_ipv4",
        "license_mode", "budget_debit_id",
    ),
    {"schema_version": 1},
)

BUDGET_LEDGER_SCHEMA = FixedRecordSchema(
    "BudgetLedger",
    (
        "schema_version", "zone", "local_date", "controlled_limit",
        "source_limit", "attempts", "previous_ledger_sha256", "continuity_state",
    ),
    {
        "schema_version": 1,
        "zone": "Asia/Shanghai",
        "controlled_limit": 10,
        "source_limit": 20,
    },
)

BUDGET_RECOVERY_RECEIPT_SCHEMA = FixedRecordSchema(
    "BudgetRecoveryReceipt",
    (
        "schema_version", "record_type", "status", "plan_sha256", "local_date",
        "unknown_reason_code", "original_bytes_present", "original_ledger_sha256",
        "recovery_debit_id", "api_main_pid", "api_process_start_ticks",
        "safe_result_code", "created_at", "updated_at",
    ),
    {"schema_version": 1, "record_type": "cmms-budget-recovery-receipt"},
)

BOOTSTRAP_RECEIPT_SCHEMA = FixedRecordSchema(
    "BootstrapReceipt",
    (
        "schema_version", "record_type", "receipt_generation", "origin_plan_sha256",
        "last_plan_sha256", "state", "super_admin_user_id", "company_id",
        "company_settings_id", "organization_admin_user_id", "role_id",
        "invitation_email_hash", "runtime_user_id", "api_key_id", "api_key_label",
        "api_key_capture_attempt_id", "api_key_capture_status", "api_key_capture_file",
        "phase2_publish_status", "phase2_env_file", "api_key_capture_cleared",
        "revoked_api_key_ids", "action_attempts", "probe_user_id",
        "action_result_codes", "created_at", "updated_at",
    ),
    {"schema_version": 1, "record_type": "cmms-bootstrap-receipt"},
)

ACCEPTANCE_RECEIPT_SCHEMA = FixedRecordSchema(
    "AcceptanceReceipt",
    (
        "schema_version", "record_type", "plan_sha256", "root_sha", "cmms_gitlink",
        "cmms_head", "api_artifact_sha256", "controller_entrypoint_sha256",
        "controller_package_sha256", "unit_generation", "api_main_pid",
        "api_process_start_ticks", "gateway_ipv4", "gateway_generation",
        "license_mode", "company_id", "organization_admin_user_id",
        "runtime_user_id", "role_id", "api_key_id", "asset_total",
        "work_order_total", "preopen_report_sha256", "preopen_check_codes",
        "postopen_report_sha256", "postopen_check_codes", "minio_probe_result_sha256",
        "minio_probe_check_codes", "minio_cleanup_code", "checked_at",
    ),
    {"schema_version": 1, "record_type": "cmms-acceptance-receipt"},
)
