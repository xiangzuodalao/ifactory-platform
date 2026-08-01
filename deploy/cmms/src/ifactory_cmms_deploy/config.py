"""Strict immutable configuration for the CMMS development controller."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import DeploymentError
from .secure_io import RuntimePathPolicy, read_secure_bytes


_MAX_ENV_BYTES = 64 * 1024
_POSTGRES_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}\Z")

_RUNTIME_KEYS = (
    "LICENSE_MODE",
    "POSTGRES_USER",
    "POSTGRES_DB",
    "POSTGRES_PASSWORD_FILE",
    "MINIO_ROOT_USER_FILE",
    "MINIO_ROOT_PASSWORD_FILE",
    "JWT_SECRET_KEY_FILE",
    "LICENSE_KEY_FILE",
    "LICENSE_FILE_PATH",
    "ALLOWED_ORGANIZATION_ADMINS",
)
_BOOTSTRAP_KEYS = (
    "ORGANIZATION_ADMIN_EMAIL",
    "RUNTIME_USER_EMAIL",
    "ROLE_EXTERNAL_ID",
    "API_KEY_LABEL",
    "SUPER_ADMIN_CURRENT_PASSWORD_FILE",
    "SUPER_ADMIN_CANDIDATE_PASSWORD_FILE",
    "ORGANIZATION_ADMIN_CURRENT_PASSWORD_FILE",
    "ORGANIZATION_ADMIN_CANDIDATE_PASSWORD_FILE",
    "RUNTIME_USER_CURRENT_PASSWORD_FILE",
    "RUNTIME_USER_CANDIDATE_PASSWORD_FILE",
)
_FRONTEND_KEYS = ("HOST", "PORT", "API_URL")
_FRONTEND_VALUES = {
    "HOST": "127.0.0.1",
    "PORT": "3001",
    "API_URL": "/api",
}


def _invalid_config() -> DeploymentError:
    return DeploymentError("CMMS-E002", "invalid CMMS runtime configuration", 2)


def _absolute_root(root: Path) -> Path:
    try:
        raw = os.fspath(root)
        absolute = Path(os.path.abspath(raw))
    except (OSError, TypeError, ValueError):
        raise _invalid_config() from None
    if not absolute.is_absolute():
        raise _invalid_config()
    return absolute


def _read_env_file(path: Path, *, runtime_root: Path) -> bytes:
    policy = RuntimePathPolicy.for_test(runtime_root, allowed_files={path})
    try:
        return read_secure_bytes(path, max_bytes=_MAX_ENV_BYTES, policy=policy)
    except DeploymentError as error:
        if error.code == "CMMS-E001":
            raise
        raise _invalid_config() from None


def _parse_exact_env(data: bytes, expected_keys: tuple[str, ...]) -> dict[str, str]:
    if type(data) is not bytes or not data or b"\x00" in data or b"\r" in data:
        raise _invalid_config()
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise _invalid_config() from None
    if not text.endswith("\n"):
        raise _invalid_config()
    rows: dict[str, str] = {}
    lines = text[:-1].split("\n")
    if not lines or any(not line for line in lines):
        raise _invalid_config()
    for line in lines:
        if line.startswith("export ") or line.count("=") < 1:
            raise _invalid_config()
        key, value = line.split("=", 1)
        if (
            key not in expected_keys
            or key in rows
            or "$" in value
            or "\n" in value
            or "\x00" in value
        ):
            raise _invalid_config()
        rows[key] = value
    if tuple(rows) != expected_keys or set(rows) != set(expected_keys):
        raise _invalid_config()
    return rows


def _canonical_secret_path(
    raw: str,
    *,
    secrets_root: Path,
    optional: bool = False,
) -> Path | None:
    if raw == "" and optional:
        return None
    if not raw:
        raise _invalid_config()
    try:
        candidate = Path(raw)
        # Check the original spelling before RuntimePathPolicy can normalize it.
        if (
            not candidate.is_absolute()
            or os.path.abspath(raw) != raw
            or os.path.normpath(raw) != raw
        ):
            raise _invalid_config()
        candidate.relative_to(secrets_root)
    except (OSError, TypeError, ValueError):
        raise _invalid_config() from None
    if candidate == secrets_root:
        raise _invalid_config()
    return candidate


def _canonical_email(value: str) -> str:
    if (
        not value
        or value != value.strip()
        or "," in value
        or value != value.casefold()
        or "@" not in value
    ):
        raise _invalid_config()
    try:
        value.encode("ascii", errors="strict")
    except UnicodeEncodeError:
        raise _invalid_config() from None
    local, domain = value.rsplit("@", 1)
    if not local or not domain or domain.startswith(".") or domain.endswith("."):
        raise _invalid_config()
    return value


@dataclass(frozen=True)
class RuntimeConfig:
    license_mode: str
    postgres_user: str
    postgres_db: str
    postgres_password_file: Path
    minio_root_user_file: Path
    minio_root_password_file: Path
    jwt_secret_key_file: Path
    license_key_file: Path
    license_file_path: Path | None
    allowed_organization_admin: str
    frontend_host: str
    frontend_port: str
    frontend_api_url: str

    compose_project: str = "ifactory-cmms-dev"
    public_browser_origin: str = "http://cmms.localhost:3000"
    host_health_origin: str = "http://127.0.0.1:3000"
    container_origin: str = "http://host.docker.internal:3000"
    api_bind: str = "127.0.0.1:8082"
    frontend_bind: str = "127.0.0.1:3001"
    postgres_bind: str = "127.0.0.1:5433"
    minio_api_bind: str = "127.0.0.1:9000"
    minio_console_bind: str = "127.0.0.1:9001"
    db_url: str = "127.0.0.1:5433/atlas"
    public_api_url: str = "http://cmms.localhost:3000/api"
    public_front_url: str = "http://cmms.localhost:3000"
    public_minio_endpoint: str = "http://cmms.localhost:3000/storage"
    storage_type: str = "MINIO"
    minio_bucket: str = "atlas-bucket"
    minio_endpoint: str = "http://127.0.0.1:9000"
    minio_region_name: str = "us-east-1"
    sigv4_service: str = "s3"
    mail_recipients: str = ""
    intercom_token: str = ""
    invitation_via_email: bool = True
    enable_email_notifications: bool = False
    enable_mail_health_check: bool = False
    enable_cors: bool = False
    rate_limit_enabled: bool = True
    enable_sso: bool = False
    ldap_enabled: bool = False
    cloud_version: bool = False
    license_fingerprint_required: bool = True
    timezone: str = "Asia/Shanghai"

    @classmethod
    def load(cls, root: Path) -> RuntimeConfig:
        repository_root = _absolute_root(root)
        runtime_root = repository_root / ".runtime"
        runtime_path = runtime_root / "cmms-development.env"
        frontend_path = runtime_root / "cmms-frontend.env"
        runtime = _parse_exact_env(
            _read_env_file(runtime_path, runtime_root=runtime_root),
            _RUNTIME_KEYS,
        )
        frontend = _parse_exact_env(
            _read_env_file(frontend_path, runtime_root=runtime_root),
            _FRONTEND_KEYS,
        )
        if frontend != _FRONTEND_VALUES:
            raise _invalid_config()
        mode = runtime["LICENSE_MODE"]
        if mode not in {"offline", "online"}:
            raise _invalid_config()
        if not _POSTGRES_IDENTIFIER.fullmatch(runtime["POSTGRES_USER"]):
            raise _invalid_config()
        if runtime["POSTGRES_DB"] != "atlas":
            raise _invalid_config()
        secrets_root = runtime_root / "secrets"
        license_file = _canonical_secret_path(
            runtime["LICENSE_FILE_PATH"],
            secrets_root=secrets_root,
            optional=True,
        )
        if (mode == "offline") != (license_file is not None):
            raise _invalid_config()
        required_paths = {
            name: _canonical_secret_path(runtime[name], secrets_root=secrets_root)
            for name in (
                "POSTGRES_PASSWORD_FILE",
                "MINIO_ROOT_USER_FILE",
                "MINIO_ROOT_PASSWORD_FILE",
                "JWT_SECRET_KEY_FILE",
                "LICENSE_KEY_FILE",
            )
        }
        return cls(
            license_mode=mode,
            postgres_user=runtime["POSTGRES_USER"],
            postgres_db=runtime["POSTGRES_DB"],
            postgres_password_file=required_paths["POSTGRES_PASSWORD_FILE"],  # type: ignore[arg-type]
            minio_root_user_file=required_paths["MINIO_ROOT_USER_FILE"],  # type: ignore[arg-type]
            minio_root_password_file=required_paths["MINIO_ROOT_PASSWORD_FILE"],  # type: ignore[arg-type]
            jwt_secret_key_file=required_paths["JWT_SECRET_KEY_FILE"],  # type: ignore[arg-type]
            license_key_file=required_paths["LICENSE_KEY_FILE"],  # type: ignore[arg-type]
            license_file_path=license_file,
            allowed_organization_admin=_canonical_email(
                runtime["ALLOWED_ORGANIZATION_ADMINS"]
            ),
            frontend_host=frontend["HOST"],
            frontend_port=frontend["PORT"],
            frontend_api_url=frontend["API_URL"],
        )

    @property
    def config_sha256(self) -> str:
        projection = {
            "allowed_organization_admin": self.allowed_organization_admin,
            "license_mode": self.license_mode,
            "postgres_db": self.postgres_db,
            "postgres_user": self.postgres_user,
            "secret_references": [
                str(self.postgres_password_file),
                str(self.minio_root_user_file),
                str(self.minio_root_password_file),
                str(self.jwt_secret_key_file),
                str(self.license_key_file),
                str(self.license_file_path) if self.license_file_path else None,
            ],
        }
        data = json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class BootstrapConfig:
    organization_admin_email: str
    runtime_user_email: str
    super_admin_current_password_file: Path | None
    super_admin_candidate_password_file: Path | None
    organization_admin_current_password_file: Path | None
    organization_admin_candidate_password_file: Path | None
    runtime_user_current_password_file: Path | None
    runtime_user_candidate_password_file: Path | None
    super_admin_email: str = "superadmin@test.com"
    role_external_id: str = "ifactory-pdm-runtime"
    api_key_label: str = "ifactory-pdm-runtime"

    @classmethod
    def load(cls, root: Path) -> BootstrapConfig:
        repository_root = _absolute_root(root)
        runtime_config = RuntimeConfig.load(repository_root)
        runtime_root = repository_root / ".runtime"
        values = _parse_exact_env(
            _read_env_file(
                runtime_root / "cmms-bootstrap.env",
                runtime_root=runtime_root,
            ),
            _BOOTSTRAP_KEYS,
        )
        if (
            values["ROLE_EXTERNAL_ID"] != "ifactory-pdm-runtime"
            or values["API_KEY_LABEL"] != "ifactory-pdm-runtime"
        ):
            raise _invalid_config()
        organization_admin = _canonical_email(values["ORGANIZATION_ADMIN_EMAIL"])
        runtime_user = _canonical_email(values["RUNTIME_USER_EMAIL"])
        identities = ("superadmin@test.com", organization_admin, runtime_user)
        if len(set(identities)) != 3:
            raise _invalid_config()
        if organization_admin != runtime_config.allowed_organization_admin:
            raise _invalid_config()
        secrets_root = runtime_root / "secrets"

        def optional(name: str) -> Path | None:
            return _canonical_secret_path(
                values[name],
                secrets_root=secrets_root,
                optional=True,
            )

        return cls(
            organization_admin_email=organization_admin,
            runtime_user_email=runtime_user,
            super_admin_current_password_file=optional(
                "SUPER_ADMIN_CURRENT_PASSWORD_FILE"
            ),
            super_admin_candidate_password_file=optional(
                "SUPER_ADMIN_CANDIDATE_PASSWORD_FILE"
            ),
            organization_admin_current_password_file=optional(
                "ORGANIZATION_ADMIN_CURRENT_PASSWORD_FILE"
            ),
            organization_admin_candidate_password_file=optional(
                "ORGANIZATION_ADMIN_CANDIDATE_PASSWORD_FILE"
            ),
            runtime_user_current_password_file=optional(
                "RUNTIME_USER_CURRENT_PASSWORD_FILE"
            ),
            runtime_user_candidate_password_file=optional(
                "RUNTIME_USER_CANDIDATE_PASSWORD_FILE"
            ),
        )
