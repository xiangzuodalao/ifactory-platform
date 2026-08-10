"""Pure CMMS gateway rendering and private fail-closed control capabilities."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any, Final, Iterator, Sequence

from .errors import DeploymentError
from .process import CommandResult, CommandRunner, CommandSpec
from .records import (
    ActionCode,
    ClaimedApplyContext,
    ConfirmedDeploymentPlan,
    DeploymentWriteLease,
    FailClosedEvidence,
    GatewayMode,
    _create_gateway_fail_closed_authority_parts,
)


__all__ = [
    "Gateway",
    "DualGatewayEvidence",
    "GatewayEvidence",
    "GatewayEvidencePurpose",
    "RenderedGateway",
    "RuntimeApiKeyRoutes",
    "gateway_listen_lines",
]

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_PUBLIC_FILE_BYTES = 256 * 1024
_RENDER_TOKEN = object()
_EVIDENCE_TOKEN = object()
_OPERATIONAL_TOKEN = object()

_EXPECTED_GITLINK: Final = "6ae7e25551ce57db9fad4620c0ff3a1be83fec01"
_EXPECTED_CLIENT_SHA256: Final = (
    "11ae82ce14b895de35cf8caf8a52d7b27ff4ea00ae41960822f0b392aed0f85d"
)
_EXPECTED_CONTRACT_SHA256: Final = (
    "c3d403630f64770facca412b29edd0bebc85451de7444a40fd6dca7f8af73dc9"
)
_EXPECTED_COMPOSE_SHA256: Final = (
    "2579565873578252f528c906c6135df669b0a1f621dd0694141362aba6d8890c"
)
_EQUIPMENT_UUID_PATTERN: Final = (
    r"^/api/assets/by-equipment-id/[0-9a-f]{8}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_IDEMPOTENCY_PATTERN: Final = (
    r"^pilot-asset:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}$"
)
_TRUSTED_CLIENT_RESIDUAL: Final = (
    "asset-create-equipment-id-body-is-trusted-client-and-confirmed-plan-"
    "responsibility-not-gateway-validation"
)
_EXPECTED_ROUTES: Final = (
    ("GET", "exact", "/api/auth/me"),
    ("POST", "exact", "/api/work-orders/search"),
    ("GET", "regex", _EQUIPMENT_UUID_PATTERN),
    ("POST", "exact", "/api/assets"),
)


def _gateway_error(message: str = "CMMS gateway operation failed safely") -> DeploymentError:
    return DeploymentError("CMMS-E042", message, 42)


def _routes_error() -> DeploymentError:
    return DeploymentError("CMMS-E043", "invalid CMMS API-key route policy", 43)


def _canonical_absolute(path: Path) -> Path:
    try:
        raw = os.fspath(path)
        value = Path(os.path.abspath(raw))
    except (OSError, TypeError, ValueError):
        raise _gateway_error() from None
    if raw != os.fspath(value):
        raise _gateway_error()
    return value


def _read_stable_public_file(path: Path, *, route_error: bool = False) -> bytes:
    error = _routes_error if route_error else _gateway_error
    fd = -1
    try:
        canonical = _canonical_absolute(path)
        fd = os.open(
            canonical,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o002
            or before.st_size <= 0
            or before.st_size > _MAX_PUBLIC_FILE_BYTES
        ):
            raise error()
        chunks: list[bytes] = []
        remaining = _MAX_PUBLIC_FILE_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 65_536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(fd)
        stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if (
            len(data) > _MAX_PUBLIC_FILE_BYTES
            or len(data) != before.st_size
            or any(getattr(before, name) != getattr(after, name) for name in stable)
        ):
            raise error()
        return data
    except DeploymentError as caught:
        if route_error and caught.code != "CMMS-E043":
            raise error() from None
        raise
    except (OSError, ValueError, TypeError):
        raise error() from None
    finally:
        if fd >= 0:
            os.close(fd)


@dataclass(frozen=True)
class RuntimeApiKeyRoute:
    method: str
    path_kind: str
    path: str


@dataclass(frozen=True)
class RuntimeApiKeyRoutes:
    platform_integration_gitlink: str
    platform_integration_client_sha256: str
    cmms_openapi_contract_sha256: str
    routes: tuple[RuntimeApiKeyRoute, ...]
    asset_create_idempotency_key_pattern: str
    trusted_client_residual: str

    @classmethod
    def load(cls, path: Path) -> "RuntimeApiKeyRoutes":
        try:
            raw = _read_stable_public_file(Path(path), route_error=True)

            def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
                value: dict[str, object] = {}
                for key, item in pairs:
                    if key in value:
                        raise _routes_error()
                    value[key] = item
                return value

            document = json.loads(
                raw.decode("utf-8", errors="strict"),
                object_pairs_hook=pairs_hook,
            )
            if type(document) is not dict or set(document) != {
                "schema_version",
                "bindings",
                "routes",
                "asset_create_idempotency_key_pattern",
                "trusted_client_residual",
            }:
                raise _routes_error()
            bindings = document["bindings"]
            rows = document["routes"]
            if (
                type(document["schema_version"]) is not int
                or document["schema_version"] != 1
                or type(bindings) is not dict
                or set(bindings) != {
                    "platform_integration_gitlink",
                    "platform_integration_client_sha256",
                    "cmms_openapi_contract_sha256",
                }
                or type(rows) is not list
                or len(rows) != len(_EXPECTED_ROUTES)
            ):
                raise _routes_error()
            actual_rows: list[tuple[str, str, str]] = []
            for row in rows:
                if (
                    type(row) is not dict
                    or set(row) != {"method", "path", "path_kind"}
                    or any(type(row[name]) is not str for name in row)
                ):
                    raise _routes_error()
                actual_rows.append((row["method"], row["path_kind"], row["path"]))
            if (
                bindings != {
                    "platform_integration_gitlink": _EXPECTED_GITLINK,
                    "platform_integration_client_sha256": _EXPECTED_CLIENT_SHA256,
                    "cmms_openapi_contract_sha256": _EXPECTED_CONTRACT_SHA256,
                }
                or tuple(actual_rows) != _EXPECTED_ROUTES
                or document["asset_create_idempotency_key_pattern"]
                != _IDEMPOTENCY_PATTERN
                or document["trusted_client_residual"] != _TRUSTED_CLIENT_RESIDUAL
            ):
                raise _routes_error()

            candidate = cls(
                platform_integration_gitlink=bindings[
                    "platform_integration_gitlink"
                ],
                platform_integration_client_sha256=bindings[
                    "platform_integration_client_sha256"
                ],
                cmms_openapi_contract_sha256=bindings[
                    "cmms_openapi_contract_sha256"
                ],
                routes=tuple(
                    RuntimeApiKeyRoute(method, kind, route)
                    for method, kind, route in actual_rows
                ),
                asset_create_idempotency_key_pattern=document[
                    "asset_create_idempotency_key_pattern"
                ],
                trusted_client_residual=document["trusted_client_residual"],
            )
            _verify_repository_bindings(Path(path), candidate)
            return candidate
        except DeploymentError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            raise _routes_error() from None


def _verify_repository_bindings(path: Path, policy: RuntimeApiKeyRoutes) -> None:
    """Verify live bytes when the manifest is in its repository-owned location."""

    canonical = _canonical_absolute(path)
    if canonical.parts[-4:] != (
        "deploy",
        "cmms",
        "manifests",
        "runtime-api-key-routes.json",
    ):
        return
    root = canonical.parents[3]
    client = root / "components/platform-integration/src/platform_integration/clients/cmms.py"
    contract = root / "contracts/openapi/cmms-integration-v1.yaml"
    if not client.exists() and not contract.exists():
        return
    if not client.is_file() or not contract.is_file():
        raise _routes_error()
    if hashlib.sha256(_read_stable_public_file(client, route_error=True)).hexdigest() != policy.platform_integration_client_sha256:
        raise _routes_error()
    if hashlib.sha256(_read_stable_public_file(contract, route_error=True)).hexdigest() != policy.cmms_openapi_contract_sha256:
        raise _routes_error()
    try:
        result = CommandRunner().run(
            CommandSpec(
                argv=(
                    "/usr/bin/git",
                    "-c",
                    "core.pager=cat",
                    "-C",
                    str(root),
                    "rev-parse",
                    "--verify",
                    "HEAD:components/platform-integration",
                ),
                cwd=root,
                environment={
                    "PATH": _FIXED_PATH,
                    "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_TERMINAL_PROMPT": "0",
                },
                timeout_seconds=15,
                stdout_limit=128,
                stderr_limit=1024,
                safe_label="cmms-route-gitlink",
            )
        )
    except DeploymentError:
        raise _routes_error() from None
    if (
        type(result) is not CommandResult
        or result.returncode != 0
        or result.stdout != f"{policy.platform_integration_gitlink}\n"
        or result.stderr != ""
    ):
        raise _routes_error()


def verified_docker_bridge_address(address: IPv4Address) -> bool:
    return (
        type(address) is IPv4Address
        and not address.is_unspecified
        and not address.is_loopback
        and not address.is_multicast
        and not address.is_link_local
        and address.is_private
        and not str(address).startswith("192.168.")
    )


def gateway_listen_lines(
    mode: GatewayMode,
    gateway_ip: IPv4Address | None,
) -> Sequence[str]:
    if type(mode) is not GatewayMode:
        raise _gateway_error("invalid gateway mode")
    lines = ("listen 127.0.0.1:3000;", "listen [::1]:3000;")
    if mode is GatewayMode.LOOPBACK:
        if gateway_ip is not None:
            raise _gateway_error("loopback gateway input is invalid")
        return lines
    if mode is not GatewayMode.DUAL:
        raise _gateway_error("invalid gateway mode")
    if gateway_ip is None or not verified_docker_bridge_address(gateway_ip):
        raise _gateway_error("Docker gateway is not verified")
    return lines + (f"listen {gateway_ip}:3000;",)


class RenderedGateway:
    __slots__ = (
        "_text",
        "_mode",
        "_gateway_ip",
        "_unit_generation",
        "_sha256",
        "_probe_token",
    )

    def __init__(
        self,
        token: object,
        text: str,
        mode: GatewayMode,
        gateway_ip: IPv4Address | None,
        unit_generation: str,
        probe_token: str,
    ) -> None:
        if token is not _RENDER_TOKEN or _HEX64.fullmatch(probe_token) is None:
            raise _gateway_error()
        self._text = text
        self._mode = mode
        self._gateway_ip = gateway_ip
        self._unit_generation = unit_generation
        self._sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self._probe_token = probe_token

    @property
    def text(self) -> str:
        return self._text

    @property
    def mode(self) -> GatewayMode:
        return self._mode

    @property
    def gateway_ip(self) -> IPv4Address | None:
        return self._gateway_ip

    @property
    def unit_generation(self) -> str:
        return self._unit_generation

    @property
    def sha256(self) -> str:
        return self._sha256

    def __eq__(self, other: object) -> bool:
        return type(other) is RenderedGateway and (
            self.text,
            self.mode,
            self.gateway_ip,
            self.unit_generation,
            self.sha256,
            self._probe_token,
        ) == (
            other.text,
            other.mode,
            other.gateway_ip,
            other.unit_generation,
            other.sha256,
            other._probe_token,
        )

    def __copy__(self) -> object:
        raise _gateway_error()

    def __deepcopy__(self, _memo: object) -> object:
        raise _gateway_error()

    def __reduce__(self) -> object:
        raise _gateway_error()


class Gateway:
    """Public render-only facade; it owns no operational capability."""

    __slots__ = ("_template", "_runner")

    def __init__(self, *, template: Path, runner: Any) -> None:
        self._template = _canonical_absolute(Path(template))
        self._runner = runner

    def render(
        self,
        mode: GatewayMode,
        gateway_ip: IPv4Address | None,
        unit_generation: str,
    ) -> RenderedGateway:
        if type(unit_generation) is not str or _HEX64.fullmatch(unit_generation) is None:
            raise _gateway_error("invalid gateway generation")
        listeners = gateway_listen_lines(mode, gateway_ip)
        raw = _read_stable_public_file(self._template)
        try:
            template = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise _gateway_error() from None
        routes_path = (
            self._template.parents[2]
            / "deploy/cmms/manifests/runtime-api-key-routes.json"
        )
        routes = RuntimeApiKeyRoutes.load(routes_path)
        policy_rows = []
        for row in routes.routes:
            if row.path_kind == "regex":
                key = f"^{row.method}:{row.path.removeprefix('^')}"
                policy_rows.append(f"        # bound route: {row.method} {row.path}")
            else:
                key = f"{row.method}:{row.path}"
            if row.path_kind == "regex":
                policy_rows.append(f'        "~{key}" 1;')
            else:
                policy_rows.append(f"        {key} 1;")
        replacements = {
            "@@LISTEN_LINES@@": "\n".join(f"        {line}" for line in listeners),
            "@@UNIT_GENERATION@@": unit_generation,
            "@@API_KEY_ROUTE_ROWS@@": "\n".join(policy_rows),
            "@@IDEMPOTENCY_PATTERN@@": routes.asset_create_idempotency_key_pattern,
        }
        if (
            any(template.count(marker) != 1 for marker in replacements)
            or template.count("@@PROBE_TOKEN@@") != 2
        ):
            raise _gateway_error()
        text = template
        for marker, value in replacements.items():
            text = text.replace(marker, value)
        probe_token = hashlib.sha256(
            b"ifactory-cmms-gateway-probe-v1\0" + text.encode("utf-8")
        ).hexdigest()
        text = text.replace("@@PROBE_TOKEN@@", probe_token)
        if "@@" in text or "{{" in text or "}}" in text or not text.endswith("\n"):
            raise _gateway_error()
        actual_listeners = tuple(
            match.group(1)
            for match in re.finditer(r"(?m)^\s*listen\s+([^;\r\n]+);\s*$", text)
        )
        expected_listeners = tuple(
            line.removeprefix("listen ").removesuffix(";")
            for line in listeners
        )
        if actual_listeners != expected_listeners:
            raise _gateway_error()
        return RenderedGateway(
            _RENDER_TOKEN,
            text,
            mode,
            gateway_ip,
            unit_generation,
            probe_token,
        )


class GatewayEvidencePurpose(StrEnum):
    PRECLAIM = "PRECLAIM"
    CLAIMED_COMPENSATION = "CLAIMED_COMPENSATION"
    EMERGENCY = "EMERGENCY"


class GatewayEvidence:
    __slots__ = (
        "_purpose",
        "_rendered",
        "_challenge",
        "_authority",
        "_gateway_ipv4",
        "_confirmed",
        "_lease",
        "_consumed",
    )

    def __init__(
        self,
        token: object,
        purpose: GatewayEvidencePurpose,
        rendered: RenderedGateway,
        challenge: object | None,
        authority: object,
        gateway_ipv4: IPv4Address,
        confirmed: ConfirmedDeploymentPlan | None = None,
        lease: DeploymentWriteLease | None = None,
    ) -> None:
        if token is not _EVIDENCE_TOKEN:
            raise _gateway_error()
        self._purpose = purpose
        self._rendered = rendered
        self._challenge = challenge
        self._authority = authority
        self._gateway_ipv4 = gateway_ipv4
        self._confirmed = confirmed
        self._lease = lease
        self._consumed = False

    @property
    def purpose(self) -> GatewayEvidencePurpose:
        return self._purpose

    @property
    def rendered(self) -> RenderedGateway:
        return self._rendered

    def __copy__(self) -> object:
        raise _gateway_error()

    def __deepcopy__(self, _memo: object) -> object:
        raise _gateway_error()

    def __reduce__(self) -> object:
        raise _gateway_error()


class DualGatewayEvidence:
    """Opaque proof that one claimed apply loaded its exact dual gateway."""

    __slots__ = ("_rendered",)

    def __init__(self, token: object, rendered: RenderedGateway) -> None:
        if (
            token is not _EVIDENCE_TOKEN
            or type(rendered) is not RenderedGateway
            or rendered.mode is not GatewayMode.DUAL
        ):
            raise _gateway_error()
        self._rendered = rendered

    @property
    def rendered(self) -> RenderedGateway:
        return self._rendered

    def __copy__(self) -> object:
        raise _gateway_error()

    def __deepcopy__(self, _memo: object) -> object:
        raise _gateway_error()

    def __reduce__(self) -> object:
        raise _gateway_error()


# The operational adapter is added below this render boundary.  Its constructor is
# already token-gated so public callers can never turn the facade into an authority.
class _ExactListenerInspector:
    __slots__ = ("_proof_mint", "_runner", "_root")

    def __init__(
        self,
        proof_mint: object,
        *,
        token: object,
        runner: Any | None = None,
        root: Path | None = None,
    ) -> None:
        if token is not _OPERATIONAL_TOKEN:
            raise _gateway_error()
        self._proof_mint = proof_mint
        self._runner = runner
        self._root = root

    def mint_absent(
        self,
        challenge: object,
        *,
        loaded: RenderedGateway,
        gateway_ipv4: IPv4Address,
        listener_count: int,
    ) -> object:
        if self._runner is None or self._root is None:
            raise _gateway_error()
        try:
            return self._proof_mint.mint_absent(  # type: ignore[attr-defined]
                challenge,
                loaded_generation=loaded.unit_generation,
                loaded_sha256=loaded.sha256,
                checked_ipv4=str(gateway_ipv4),
                checked_port=3000,
                listener_count=listener_count,
            )
        except (AttributeError, DeploymentError, TypeError, ValueError):
            raise _gateway_error() from None


_NGINX_IMAGE = (
    "nginx:1.27.0-alpine@sha256:"
    "a377278b7dde3a8012b25d141d025a88dbf9f5ed13c5cdf21ee241e7ec07ab57"
)
_CONFIG_NAME = "nginx.conf"
_OPERATION_LOCK_NAME = "cmms-gateway-operation.lock"
_FIXED_PATH = "/usr/bin:/bin"
_SAFE_REASON = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_F_ADD_SEALS = 1033
_F_GET_SEALS = 1034
_FULL_MEMFD_SEALS = 0x0001 | 0x0002 | 0x0004 | 0x0008


def _validate_safe_reason(reason: str) -> None:
    if type(reason) is not str or _SAFE_REASON.fullmatch(reason) is None:
        raise _gateway_error()


def _strict_command_result(
    result: object,
    *,
    allow_stdout: bool = False,
) -> CommandResult:
    if (
        type(result) is not CommandResult
        or type(result.returncode) is not int
        or result.returncode != 0
        or type(result.stdout) is not str
        or type(result.stderr) is not str
        or result.stderr != ""
        or (not allow_stdout and result.stdout != "")
    ):
        raise _gateway_error()
    return result


def _run_fixed(
    runner: Any,
    *,
    argv: tuple[str, ...],
    cwd: Path,
    safe_label: str,
    allow_stdout: bool = False,
    environment: dict[str, str] | None = None,
) -> CommandResult:
    try:
        result = runner.run(
            CommandSpec(
                argv=argv,
                cwd=cwd,
                environment={"PATH": _FIXED_PATH} if environment is None else environment,
                timeout_seconds=30,
                stdout_limit=64 * 1024 if allow_stdout else 0,
                stderr_limit=16 * 1024,
                safe_label=safe_label,
            )
        )
        return _strict_command_result(result, allow_stdout=allow_stdout)
    except DeploymentError:
        raise _gateway_error() from None
    except Exception:
        raise _gateway_error() from None


@contextmanager
def _empty_gateway_docker_config() -> Iterator[str]:
    directory = Path("/")
    directory_fd = -1
    created = False
    cleanup_failed = False
    identity: tuple[int, int] | None = None
    try:
        directory = Path(
            tempfile.mkdtemp(
                prefix="ifactory-cmms-gateway-docker-",
                dir="/tmp",
            )
        )
        created = True
        os.chmod(directory, 0o700)
        directory_fd = os.open(
            directory,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        metadata = os.fstat(directory_fd)
        identity = (metadata.st_dev, metadata.st_ino)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or os.listdir(directory_fd)
        ):
            raise _gateway_error()
        yield f"/proc/{os.getpid()}/fd/{directory_fd}"
    except DeploymentError:
        raise
    except (OSError, TypeError, ValueError):
        raise _gateway_error() from None
    finally:
        if directory_fd >= 0 and created and identity is not None:
            try:
                after = os.fstat(directory_fd)
                lexical = directory.stat(follow_symlinks=False)
                if (
                    (after.st_dev, after.st_ino) != identity
                    or (lexical.st_dev, lexical.st_ino) != identity
                    or not stat.S_ISDIR(after.st_mode)
                    or after.st_uid != os.getuid()
                    or stat.S_IMODE(after.st_mode) != 0o700
                    or os.listdir(directory_fd)
                ):
                    cleanup_failed = True
            except (OSError, TypeError, ValueError):
                cleanup_failed = True
        if directory_fd >= 0:
            try:
                os.close(directory_fd)
            except OSError:
                cleanup_failed = True
        if created:
            try:
                directory.rmdir()
            except OSError:
                cleanup_failed = True
        if cleanup_failed:
            raise _gateway_error()


def _run_docker(
    runner: Any,
    *,
    argv: tuple[str, ...],
    cwd: Path,
    safe_label: str,
    allow_stdout: bool = False,
) -> CommandResult:
    with _empty_gateway_docker_config() as docker_config:
        return _run_fixed(
            runner,
            argv=argv,
            cwd=cwd,
            safe_label=safe_label,
            allow_stdout=allow_stdout,
            environment={
                "PATH": _FIXED_PATH,
                "DOCKER_CONFIG": docker_config,
            },
        )


def _open_gateway_runtime(root: Path) -> tuple[int, Path]:
    root_fd = -1
    runtime_fd = -1
    gateway_fd = -1
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        root_metadata = os.fstat(root_fd)
        if not stat.S_ISDIR(root_metadata.st_mode) or root_metadata.st_uid != os.getuid():
            raise _gateway_error()
        runtime_fd = os.open(
            ".runtime",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=root_fd,
        )
        runtime_metadata = os.fstat(runtime_fd)
        if (
            not stat.S_ISDIR(runtime_metadata.st_mode)
            or runtime_metadata.st_uid != os.getuid()
            or stat.S_IMODE(runtime_metadata.st_mode) != 0o700
        ):
            raise _gateway_error()
        created = False
        try:
            os.mkdir("cmms-nginx", 0o755, dir_fd=runtime_fd)
            created = True
            os.fsync(runtime_fd)
        except FileExistsError:
            pass
        gateway_fd = os.open(
            "cmms-nginx",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=runtime_fd,
        )
        if created:
            os.fchmod(gateway_fd, 0o755)
            os.fsync(gateway_fd)
            os.fsync(runtime_fd)
        gateway_metadata = os.fstat(gateway_fd)
        if (
            not stat.S_ISDIR(gateway_metadata.st_mode)
            or gateway_metadata.st_uid != os.getuid()
            or stat.S_IMODE(gateway_metadata.st_mode) != 0o755
        ):
            raise _gateway_error()
        result = gateway_fd
        gateway_fd = -1
        return result, root / ".runtime/cmms-nginx"
    except DeploymentError:
        raise
    except OSError:
        raise _gateway_error() from None
    finally:
        if gateway_fd >= 0:
            os.close(gateway_fd)
        if runtime_fd >= 0:
            os.close(runtime_fd)
        if root_fd >= 0:
            os.close(root_fd)


@contextmanager
def _gateway_operation_lock(root: Path) -> Iterator[None]:
    root_fd = -1
    runtime_fd = -1
    lock_fd = -1
    locked = False
    try:
        root_fd = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        runtime_fd = os.open(
            ".runtime",
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=root_fd,
        )
        runtime_metadata = os.fstat(runtime_fd)
        if (
            not stat.S_ISDIR(runtime_metadata.st_mode)
            or runtime_metadata.st_uid != os.getuid()
            or stat.S_IMODE(runtime_metadata.st_mode) != 0o700
        ):
            raise _gateway_error()
        lock_fd = os.open(
            _OPERATION_LOCK_NAME,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=runtime_fd,
        )
        lock_metadata = os.fstat(lock_fd)
        lexical = os.stat(
            _OPERATION_LOCK_NAME,
            dir_fd=runtime_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(lock_metadata.st_mode)
            or lock_metadata.st_uid != os.getuid()
            or lock_metadata.st_nlink != 1
            or stat.S_IMODE(lock_metadata.st_mode) != 0o600
            or lock_metadata.st_size != 0
            or (lock_metadata.st_dev, lock_metadata.st_ino)
            != (lexical.st_dev, lexical.st_ino)
        ):
            raise _gateway_error()
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        after = os.fstat(lock_fd)
        lexical_after = os.stat(
            _OPERATION_LOCK_NAME,
            dir_fd=runtime_fd,
            follow_symlinks=False,
        )
        if (
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (
                lock_metadata.st_dev,
                lock_metadata.st_ino,
                lock_metadata.st_size,
                lock_metadata.st_mtime_ns,
            )
            or (after.st_dev, after.st_ino)
            != (lexical_after.st_dev, lexical_after.st_ino)
        ):
            raise _gateway_error()
        try:
            yield
        finally:
            final = os.fstat(lock_fd)
            lexical_final = os.stat(
                _OPERATION_LOCK_NAME,
                dir_fd=runtime_fd,
                follow_symlinks=False,
            )
            if (
                (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or (final.st_dev, final.st_ino)
                != (lexical_final.st_dev, lexical_final.st_ino)
            ):
                raise _gateway_error()
    except DeploymentError:
        raise
    except (OSError, ValueError):
        raise _gateway_error() from None
    finally:
        if locked:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
        if lock_fd >= 0:
            os.close(lock_fd)
        if runtime_fd >= 0:
            os.close(runtime_fd)
        if root_fd >= 0:
            os.close(root_fd)


def _write_all(fd: int, data: bytes) -> None:
    offset = 0
    try:
        while offset < len(data):
            written = os.write(fd, data[offset:])
            if written <= 0:
                raise _gateway_error()
            offset += written
    except DeploymentError:
        raise
    except OSError:
        raise _gateway_error() from None


def _open_sealed_memfd(name: str, data: bytes) -> int:
    fd = -1
    try:
        flags = os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
        fd = os.memfd_create(name, flags)
        os.fchmod(fd, 0o600)
        _write_all(fd, data)
        os.lseek(fd, 0, os.SEEK_SET)
        fcntl.fcntl(fd, _F_ADD_SEALS, _FULL_MEMFD_SEALS)
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size != len(data)
            or fcntl.fcntl(fd, _F_GET_SEALS) & _FULL_MEMFD_SEALS
            != _FULL_MEMFD_SEALS
        ):
            raise _gateway_error()
        result = fd
        fd = -1
        return result
    except DeploymentError:
        raise
    except (AttributeError, OSError, ValueError):
        raise _gateway_error() from None
    finally:
        if fd >= 0:
            os.close(fd)


def _read_exact_config_fd(fd: int, expected_size: int) -> bytes:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = expected_size + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 65_536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
    except OSError:
        raise _gateway_error() from None
    if len(data) != expected_size:
        raise _gateway_error()
    return data


def _require_config_metadata(metadata: os.stat_result, *, size: int) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o444
        or metadata.st_size != size
    ):
        raise _gateway_error()


def _validate_nginx_candidate(
    runner: Any,
    root: Path,
    candidate: Path,
) -> None:
    _run_docker(
        runner,
        argv=(
            "/usr/bin/docker",
            "--host",
            "unix:///var/run/docker.sock",
            "run",
            "--pull",
            "never",
            "--rm",
            "--network",
            "none",
            "--platform",
            "linux/amd64",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "101:101",
            "--tmpfs",
            "/tmp:uid=101,gid=101,mode=0700",
            "--entrypoint",
            "/usr/sbin/nginx",
            "--mount",
            f"type=bind,source={candidate},target=/etc/ifactory-cmms/nginx.conf,readonly",
            _NGINX_IMAGE,
            "-t",
            "-q",
            "-c",
            "/etc/ifactory-cmms/nginx.conf",
        ),
        cwd=root,
        safe_label="cmms-gateway-validate",
    )


def _publish_rendered(
    runner: Any,
    root: Path,
    rendered: RenderedGateway,
    *,
    validate_candidate: bool = True,
) -> Path:
    data = rendered.text.encode("utf-8")
    if not data or len(data) > _MAX_PUBLIC_FILE_BYTES:
        raise _gateway_error()
    directory_fd, runtime_directory = _open_gateway_runtime(root)
    temporary_name = f".{_CONFIG_NAME}.candidate-{secrets.token_hex(16)}"
    temporary_fd = -1
    temporary_exists = False
    try:
        temporary_fd = os.open(
            temporary_name,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | os.O_CLOEXEC
            | os.O_NOFOLLOW
            | os.O_NONBLOCK,
            0o444,
            dir_fd=directory_fd,
        )
        temporary_exists = True
        os.fchmod(temporary_fd, 0o444)
        _write_all(temporary_fd, data)
        os.fsync(temporary_fd)
        candidate_metadata = os.fstat(temporary_fd)
        _require_config_metadata(candidate_metadata, size=len(data))
        candidate_identity = (candidate_metadata.st_dev, candidate_metadata.st_ino)
        if validate_candidate:
            _validate_nginx_candidate(
                runner,
                root,
                runtime_directory / temporary_name,
            )
        after_check = os.fstat(temporary_fd)
        _require_config_metadata(after_check, size=len(data))
        if (
            (after_check.st_dev, after_check.st_ino) != candidate_identity
            or _read_exact_config_fd(temporary_fd, len(data)) != data
        ):
            raise _gateway_error()

        existing_fd = -1
        try:
            try:
                existing_fd = os.open(
                    _CONFIG_NAME,
                    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                pass
            if existing_fd >= 0:
                existing = os.fstat(existing_fd)
                _require_config_metadata(existing, size=existing.st_size)
        finally:
            if existing_fd >= 0:
                os.close(existing_fd)

        os.replace(
            temporary_name,
            _CONFIG_NAME,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_exists = False
        os.fsync(directory_fd)
        published_fd = os.open(
            _CONFIG_NAME,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
        try:
            published = os.fstat(published_fd)
            _require_config_metadata(published, size=len(data))
            if (
                (published.st_dev, published.st_ino) != candidate_identity
                or _read_exact_config_fd(published_fd, len(data)) != data
            ):
                raise _gateway_error()
        finally:
            os.close(published_fd)
        return runtime_directory / _CONFIG_NAME
    except DeploymentError:
        raise
    except OSError:
        raise _gateway_error() from None
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if temporary_exists:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            except OSError:
                pass
        os.close(directory_fd)


@dataclass(frozen=True)
class _LoadedConfigBinding:
    dev: int
    ino: int
    size: int
    mtime_ns: int
    ctime_ns: int
    sha256: str


def _loaded_config_binding(
    root: Path,
    expected: RenderedGateway,
) -> _LoadedConfigBinding:
    data = expected.text.encode("utf-8")
    directory_fd, _runtime_directory = _open_gateway_runtime(root)
    fd = -1
    try:
        fd = os.open(
            _CONFIG_NAME,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
        before = os.fstat(fd)
        _require_config_metadata(before, size=len(data))
        loaded = _read_exact_config_fd(fd, len(data))
        after = os.fstat(fd)
        _require_config_metadata(after, size=len(data))
        stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, name) != getattr(after, name) for name in stable) or loaded != data:
            raise _gateway_error()
        lexical = os.stat(
            _CONFIG_NAME,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        _require_config_metadata(lexical, size=len(data))
        if (lexical.st_dev, lexical.st_ino) != (after.st_dev, after.st_ino):
            raise _gateway_error()
        return _LoadedConfigBinding(
            dev=after.st_dev,
            ino=after.st_ino,
            size=after.st_size,
            mtime_ns=after.st_mtime_ns,
            ctime_ns=after.st_ctime_ns,
            sha256=hashlib.sha256(loaded).hexdigest(),
        )
    except DeploymentError:
        raise
    except OSError:
        raise _gateway_error() from None
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(directory_fd)


def _reopen_rendered(root: Path, expected: RenderedGateway) -> RenderedGateway:
    binding = _loaded_config_binding(root, expected)
    if binding.sha256 != expected.sha256:
        raise _gateway_error()
    return expected


def _compose_snapshot_bytes(root: Path, nginx_runtime: Path) -> bytes:
    from .config import RuntimeConfig

    config = RuntimeConfig.load(root)
    rows = (
        ("POSTGRES_USER", config.postgres_user),
        ("POSTGRES_DB", config.postgres_db),
        ("POSTGRES_PASSWORD_FILE", str(config.postgres_password_file)),
        ("MINIO_ROOT_USER_FILE", str(config.minio_root_user_file)),
        ("MINIO_ROOT_PASSWORD_FILE", str(config.minio_root_password_file)),
        ("CMMS_NGINX_RUNTIME_DIR", str(nginx_runtime)),
    )
    return ("".join(f"{key}={value}\n" for key, value in rows)).encode("utf-8")


def _reviewed_compose_path(root: Path) -> Path:
    path = root / "deploy/compose/cmms-development.yml"
    data = _read_stable_public_file(path)
    if hashlib.sha256(data).hexdigest() != _EXPECTED_COMPOSE_SHA256:
        raise _gateway_error()
    return path


def _run_compose(
    runner: Any,
    root: Path,
    controller_pid: int,
    suffix: tuple[str, ...],
    *,
    safe_label: str,
    allow_stdout: bool = False,
) -> CommandResult:
    if controller_pid != os.getpid():
        raise _gateway_error()
    runtime_directory = root / ".runtime/cmms-nginx"
    compose_path = _reviewed_compose_path(root)
    snapshot = _compose_snapshot_bytes(root, runtime_directory)
    fd = -1
    try:
        fd = _open_sealed_memfd("ifactory-cmms-compose-env", snapshot)
        return _run_docker(
            runner,
            argv=(
                "/usr/bin/docker",
                "--host",
                "unix:///var/run/docker.sock",
                "compose",
                "--project-name",
                "ifactory-cmms-dev",
                "--env-file",
                f"/proc/{controller_pid}/fd/{fd}",
                "-f",
                str(compose_path),
                *suffix,
            ),
            cwd=root,
            safe_label=safe_label,
            allow_stdout=allow_stdout,
        )
    except (AttributeError, OSError, ValueError):
        raise _gateway_error() from None
    finally:
        if fd >= 0:
            os.close(fd)


def _stop_exact_nginx_container(
    runner: Any,
    root: Path,
) -> None:
    try:
        listed = _run_docker(
            runner,
            argv=(
                "/usr/bin/docker",
                "--host",
                "unix:///var/run/docker.sock",
                "container",
                "ls",
                "--all",
                "--no-trunc",
                "--quiet",
                "--filter",
                "label=com.docker.compose.project=ifactory-cmms-dev",
                "--filter",
                "label=com.docker.compose.service=nginx",
                "--filter",
                "label=com.docker.compose.oneoff=False",
            ),
            cwd=root,
            safe_label="cmms-gateway-recovery-list",
            allow_stdout=True,
        )
        if listed.stdout == "":
            return
        if re.fullmatch(r"[0-9a-f]{64}\n", listed.stdout) is None:
            raise _gateway_error()
        container_id = listed.stdout[:-1]
        inspected = _run_docker(
            runner,
            argv=(
                "/usr/bin/docker",
                "--host",
                "unix:///var/run/docker.sock",
                "container",
                "inspect",
                container_id,
            ),
            cwd=root,
            safe_label="cmms-gateway-recovery-inspect",
            allow_stdout=True,
        )

        def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if type(key) is not str or key in result:
                    raise _gateway_error()
                result[key] = value
            return result

        document = json.loads(
            inspected.stdout,
            object_pairs_hook=pairs_hook,
            parse_constant=lambda _value: (_ for _ in ()).throw(_gateway_error()),
        )
        if type(document) is not list or len(document) != 1:
            raise _gateway_error()
        row = document[0]
        if type(row) is not dict:
            raise _gateway_error()
        config = row.get("Config")
        state = row.get("State")
        host_config = row.get("HostConfig")
        if (
            type(config) is not dict
            or type(state) is not dict
            or type(host_config) is not dict
        ):
            raise _gateway_error()
        labels = config.get("Labels")
        required_labels = {
            "com.docker.compose.project": "ifactory-cmms-dev",
            "com.docker.compose.service": "nginx",
            "com.docker.compose.oneoff": "False",
            "com.docker.compose.container-number": "1",
        }
        if (
            row.get("Id") != container_id
            or row.get("Name") != "/ifactory-cmms-dev-nginx-1"
            or config.get("Image") != _NGINX_IMAGE
            or type(labels) is not dict
            or any(labels.get(key) != value for key, value in required_labels.items())
            or host_config.get("NetworkMode") != "host"
            or type(state.get("Running")) is not bool
        ):
            raise _gateway_error()
        if not state["Running"]:
            return
        stopped = _run_docker(
            runner,
            argv=(
                "/usr/bin/docker",
                "--host",
                "unix:///var/run/docker.sock",
                "container",
                "stop",
                "--signal",
                "SIGTERM",
                "--timeout",
                "10",
                container_id,
            ),
            cwd=root,
            safe_label="cmms-gateway-recovery-stop",
            allow_stdout=True,
        )
        if stopped.stdout != f"{container_id}\n":
            raise _gateway_error()
    except DeploymentError:
        raise
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
        raise _gateway_error() from None


def _nginx_running(
    runner: Any,
    root: Path,
    controller_pid: int,
) -> bool:
    result = _run_compose(
        runner,
        root,
        controller_pid,
        ("ps", "--status", "running", "--services", "nginx"),
        safe_label="cmms-gateway-status",
        allow_stdout=True,
    )
    if result.stdout == "":
        return False
    if result.stdout == "nginx\n":
        return True
    raise _gateway_error()


def _reload_nginx(runner: Any, root: Path, controller_pid: int) -> None:
    _run_compose(
        runner,
        root,
        controller_pid,
        (
            "exec",
            "--no-TTY",
            "nginx",
            "/bin/kill",
            "-HUP",
            "1",
        ),
        safe_label="cmms-gateway-reload",
    )


def _stop_nginx(runner: Any, root: Path, controller_pid: int) -> None:
    _run_compose(
        runner,
        root,
        controller_pid,
        ("--progress", "quiet", "stop", "nginx"),
        safe_label="cmms-gateway-stop",
    )


def _exact_external_listener_count(
    runner: Any,
    root: Path,
    gateway_ipv4: IPv4Address | None,
) -> int:
    result = _run_fixed(
        runner,
        argv=("/usr/bin/ss", "-H", "-ltn", "sport", "=", ":3000"),
        cwd=root,
        safe_label="cmms-gateway-listener-inspect",
        allow_stdout=True,
    )
    count = 0
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0] != "LISTEN":
            raise _gateway_error()
        local = fields[3]
        if gateway_ipv4 is not None and local == f"{gateway_ipv4}:3000":
            count += 1
        elif local not in {"127.0.0.1:3000", "[::1]:3000"}:
            raise _gateway_error()
    return count


def _probe_loaded_dual_gateway(
    runner: Any,
    root: Path,
    gateway_ipv4: IPv4Address,
    rendered: RenderedGateway,
) -> None:
    if (
        type(gateway_ipv4) is not IPv4Address
        or type(rendered) is not RenderedGateway
        or rendered.mode is not GatewayMode.DUAL
        or rendered.gateway_ip != gateway_ipv4
        or _HEX64.fullmatch(rendered._probe_token) is None
    ):
        raise _gateway_error()
    expected = f"{rendered._probe_token}\n200\n"
    for label, address in (
        ("loopback", IPv4Address("127.0.0.1")),
        ("gateway", gateway_ipv4),
    ):
        result = _run_fixed(
            runner,
            argv=(
                "/usr/bin/curl",
                "--silent",
                "--show-error",
                "--fail",
                "--connect-timeout",
                "2",
                "--max-time",
                "5",
                "--max-redirs",
                "0",
                "--noproxy",
                "*",
                "--proto",
                "=http",
                "--request",
                "GET",
                "--header",
                "Host: cmms.localhost",
                "--write-out",
                "\n%{http_code}\n",
                (
                    f"http://{address}:3000/"
                    f"__ifactory_cmms_gateway_probe/{rendered._probe_token}"
                ),
            ),
            cwd=root,
            safe_label=f"cmms-gateway-probe-{label}",
            allow_stdout=True,
        )
        if result.stdout != expected:
            raise _gateway_error()


def _resolve_gateway(runner: Any) -> IPv4Address:
    try:
        from .compose import resolve_default_bridge_gateway

        address = resolve_default_bridge_gateway(runner)
    except DeploymentError:
        raise _gateway_error() from None
    except (ImportError, TypeError, ValueError):
        raise _gateway_error() from None
    if type(address) is not IPv4Address or not verified_docker_bridge_address(address):
        raise _gateway_error()
    return address


class _OperationalGateway(Gateway):
    __slots__ = (
        "_root",
        "_controller_pid",
        "_challenge_issuer",
        "_evidence_issuer",
        "_listener_inspector",
        "_authority_identity",
    )

    def __init__(
        self,
        facade: Gateway,
        *,
        token: object,
        root: Path | None = None,
        controller_pid: int | None = None,
        challenge_issuer: object | None = None,
        evidence_issuer: object | None = None,
        listener_inspector: _ExactListenerInspector | None = None,
    ) -> None:
        if token is not _OPERATIONAL_TOKEN or type(facade) is not Gateway:
            raise _gateway_error()
        if (
            root is None
            or challenge_issuer is None
            or evidence_issuer is None
            or type(listener_inspector) is not _ExactListenerInspector
        ):
            raise _gateway_error()
        super().__init__(template=facade._template, runner=facade._runner)
        self._root = _canonical_absolute(root)
        if not self._root.is_dir():
            raise _gateway_error()
        selected_pid = os.getpid() if controller_pid is None else controller_pid
        if type(selected_pid) is not int or selected_pid != os.getpid():
            raise _gateway_error()
        self._controller_pid = selected_pid
        self._challenge_issuer = challenge_issuer
        self._evidence_issuer = evidence_issuer
        self._listener_inspector = listener_inspector
        self._authority_identity = object()

    def _render_loopback(
        self,
        unit_generation: str,
    ) -> tuple[RenderedGateway, IPv4Address]:
        gateway_ipv4 = _resolve_gateway(self._runner)
        rendered = self.render(
            mode=GatewayMode.LOOPBACK,
            gateway_ip=None,
            unit_generation=unit_generation,
        )
        return rendered, gateway_ipv4

    def _publish_and_reload(
        self,
        rendered: RenderedGateway,
        *,
        cold_preclaim: bool = False,
        gateway_ipv4: IPv4Address | None = None,
    ) -> None:
        runtime_fd, _runtime_path = _open_gateway_runtime(self._root)
        os.close(runtime_fd)
        was_running = _nginx_running(
            self._runner,
            self._root,
            self._controller_pid,
        )
        if cold_preclaim:
            if type(gateway_ipv4) is not IPv4Address:
                raise _gateway_error()
            if was_running:
                _stop_nginx(self._runner, self._root, self._controller_pid)
            _publish_rendered(
                self._runner,
                self._root,
                rendered,
                validate_candidate=False,
            )
            _reopen_rendered(self._root, rendered)
            if _exact_external_listener_count(
                self._runner,
                self._root,
                gateway_ipv4,
            ) != 0:
                raise _gateway_error()
            return
        _publish_rendered(self._runner, self._root, rendered)
        if was_running:
            _reload_nginx(self._runner, self._root, self._controller_pid)
        _reopen_rendered(self._root, rendered)

    def _recover_closed(self, gateway_ipv4: IPv4Address | None) -> None:
        stop_ok = False
        try:
            _stop_nginx(self._runner, self._root, self._controller_pid)
            stop_ok = True
        except DeploymentError:
            try:
                _stop_exact_nginx_container(
                    self._runner,
                    self._root,
                )
                stop_ok = True
            except DeploymentError:
                pass
        try:
            absent = (
                _exact_external_listener_count(
                    self._runner,
                    self._root,
                    gateway_ipv4,
                )
                == 0
            )
        except DeploymentError:
            absent = False
        if not stop_ok or not absent:
            raise _gateway_error()

    def begin_fail_closed(
        self,
        plan: ConfirmedDeploymentPlan,
        lease: DeploymentWriteLease,
        reason: str,
    ) -> GatewayEvidence:
        _validate_safe_reason(reason)
        if type(plan) is not ConfirmedDeploymentPlan or type(lease) is not DeploymentWriteLease:
            raise _gateway_error()
        gateway_ipv4: IPv4Address | None = None
        try:
            with _gateway_operation_lock(self._root):
                try:
                    rendered, gateway_ipv4 = self._render_loopback(
                        plan.plan.snapshot.unit_generation
                    )
                    challenge = self._challenge_issuer.begin(  # type: ignore[attr-defined]
                        plan,
                        lease,
                        rendered.unit_generation,
                        rendered.sha256,
                        str(gateway_ipv4),
                    )
                    cold_preclaim = any(
                        action.code is ActionCode.IMAGES_PULL_EXACT
                        for action in plan.plan.actions
                    )
                    self._publish_and_reload(
                        rendered,
                        cold_preclaim=cold_preclaim,
                        gateway_ipv4=gateway_ipv4,
                    )
                    return GatewayEvidence(
                        _EVIDENCE_TOKEN,
                        GatewayEvidencePurpose.PRECLAIM,
                        rendered,
                        challenge,
                        self._authority_identity,
                        gateway_ipv4,
                        plan,
                        lease,
                    )
                except (AttributeError, DeploymentError, TypeError, ValueError):
                    self._recover_closed(gateway_ipv4)
                    raise _gateway_error() from None
        except DeploymentError:
            try:
                self._recover_closed(gateway_ipv4)
            except DeploymentError:
                pass
            raise _gateway_error() from None

    def require_external_listener_absent(
        self,
        evidence: GatewayEvidence,
    ) -> FailClosedEvidence:
        if (
            type(evidence) is not GatewayEvidence
            or evidence._authority is not self._authority_identity
            or evidence._purpose is not GatewayEvidencePurpose.PRECLAIM
            or evidence._consumed
            or evidence._challenge is None
        ):
            raise _gateway_error()
        try:
            with _gateway_operation_lock(self._root):
                try:
                    _reopen_rendered(self._root, evidence._rendered)
                    before_probe = _loaded_config_binding(
                        self._root,
                        evidence._rendered,
                    )
                    listener_count = _exact_external_listener_count(
                        self._runner,
                        self._root,
                        evidence._gateway_ipv4,
                    )
                    loaded_after_probe = _reopen_rendered(
                        self._root,
                        evidence._rendered,
                    )
                    after_probe = _loaded_config_binding(
                        self._root,
                        evidence._rendered,
                    )
                    if before_probe != after_probe:
                        raise _gateway_error()
                    proof = self._listener_inspector.mint_absent(
                        evidence._challenge,
                        loaded=loaded_after_probe,
                        gateway_ipv4=evidence._gateway_ipv4,
                        listener_count=listener_count,
                    )
                    if (
                        type(evidence._confirmed) is not ConfirmedDeploymentPlan
                        or type(evidence._lease) is not DeploymentWriteLease
                    ):
                        raise _gateway_error()
                    result = self._evidence_issuer.issue(  # type: ignore[attr-defined]
                        evidence._confirmed,
                        evidence._lease,
                        proof,
                    )
                    evidence._consumed = True
                    return result
                except (AttributeError, DeploymentError, TypeError, ValueError):
                    self._recover_closed(evidence._gateway_ipv4)
                    raise _gateway_error() from None
        except DeploymentError:
            try:
                self._recover_closed(evidence._gateway_ipv4)
            except DeploymentError:
                pass
            raise _gateway_error() from None

    def fail_closed_claimed(
        self,
        context: ClaimedApplyContext,
        reason: str,
    ) -> GatewayEvidence:
        _validate_safe_reason(reason)
        if type(context) is not ClaimedApplyContext:
            raise _gateway_error()
        try:
            context.require_action(ActionCode.GATEWAY_FAIL_CLOSED)
        except DeploymentError:
            raise _gateway_error() from None
        gateway_ipv4: IPv4Address | None = None
        try:
            with _gateway_operation_lock(self._root):
                try:
                    context.require_action(ActionCode.GATEWAY_FAIL_CLOSED)
                    rendered, gateway_ipv4 = self._render_loopback(
                        context.plan.snapshot.unit_generation
                    )
                    self._publish_and_reload(rendered)
                    before_probe = _loaded_config_binding(self._root, rendered)
                    listener_count = _exact_external_listener_count(
                        self._runner,
                        self._root,
                        gateway_ipv4,
                    )
                    _reopen_rendered(self._root, rendered)
                    after_probe = _loaded_config_binding(self._root, rendered)
                    if listener_count != 0 or before_probe != after_probe:
                        raise _gateway_error()
                    context.require_action(ActionCode.GATEWAY_FAIL_CLOSED)
                    return GatewayEvidence(
                        _EVIDENCE_TOKEN,
                        GatewayEvidencePurpose.CLAIMED_COMPENSATION,
                        rendered,
                        None,
                        self._authority_identity,
                        gateway_ipv4,
                    )
                except (DeploymentError, TypeError, ValueError):
                    self._recover_closed(gateway_ipv4)
                    raise _gateway_error() from None
        except DeploymentError:
            try:
                self._recover_closed(gateway_ipv4)
            except DeploymentError:
                pass
            raise _gateway_error() from None

    def emergency_fail_closed(self, reason: str) -> GatewayEvidence:
        _validate_safe_reason(reason)
        gateway_ipv4: IPv4Address | None = None
        try:
            with _gateway_operation_lock(self._root):
                try:
                    gateway_ipv4 = _resolve_gateway(self._runner)
                    from .config import RuntimeConfig

                    config = RuntimeConfig.load(self._root)
                    del config
                    generation = hashlib.sha256(
                        b"ifactory-cmms-emergency-loopback-v1"
                    ).hexdigest()
                    rendered = self.render(GatewayMode.LOOPBACK, None, generation)
                    self._publish_and_reload(rendered)
                    before_probe = _loaded_config_binding(self._root, rendered)
                    listener_count = _exact_external_listener_count(
                        self._runner,
                        self._root,
                        gateway_ipv4,
                    )
                    _reopen_rendered(self._root, rendered)
                    after_probe = _loaded_config_binding(self._root, rendered)
                    if listener_count != 0 or before_probe != after_probe:
                        raise _gateway_error()
                    return GatewayEvidence(
                        _EVIDENCE_TOKEN,
                        GatewayEvidencePurpose.EMERGENCY,
                        rendered,
                        None,
                        self._authority_identity,
                        gateway_ipv4,
                    )
                except (DeploymentError, ImportError, TypeError, ValueError):
                    self._recover_closed(gateway_ipv4)
                    raise _gateway_error() from None
        except DeploymentError:
            try:
                self._recover_closed(gateway_ipv4)
            except DeploymentError:
                pass
            raise _gateway_error() from None

    def enable_dual(
        self,
        context: ClaimedApplyContext,
        expected: RenderedGateway,
    ) -> DualGatewayEvidence:
        if type(context) is not ClaimedApplyContext or type(expected) is not RenderedGateway:
            raise _gateway_error()
        try:
            context.require_action(ActionCode.GATEWAY_ENABLE_DUAL)
        except DeploymentError:
            raise _gateway_error() from None
        gateway_ipv4: IPv4Address | None = None
        try:
            with _gateway_operation_lock(self._root):
                try:
                    context.require_action(ActionCode.GATEWAY_ENABLE_DUAL)
                    gateway_ipv4 = _resolve_gateway(self._runner)
                    rendered = self.render(
                        GatewayMode.DUAL,
                        gateway_ipv4,
                        context.plan.snapshot.unit_generation,
                    )
                    if rendered != expected:
                        raise _gateway_error()
                    self._publish_and_reload(rendered)
                    before_probe = _loaded_config_binding(self._root, rendered)
                    listener_count = _exact_external_listener_count(
                        self._runner,
                        self._root,
                        gateway_ipv4,
                    )
                    bridge_before_probes = _resolve_gateway(self._runner)
                    if bridge_before_probes != gateway_ipv4:
                        raise _gateway_error()
                    _probe_loaded_dual_gateway(
                        self._runner,
                        self._root,
                        gateway_ipv4,
                        rendered,
                    )
                    bridge_after_probes = _resolve_gateway(self._runner)
                    _reopen_rendered(self._root, rendered)
                    after_probe = _loaded_config_binding(self._root, rendered)
                    if (
                        listener_count != 1
                        or bridge_after_probes != gateway_ipv4
                        or before_probe != after_probe
                    ):
                        raise _gateway_error()
                    context.require_action(ActionCode.GATEWAY_ENABLE_DUAL)
                    return DualGatewayEvidence(_EVIDENCE_TOKEN, rendered)
                except (DeploymentError, TypeError, ValueError):
                    self._recover_closed(gateway_ipv4)
                    raise _gateway_error() from None
        except DeploymentError:
            try:
                self._recover_closed(gateway_ipv4)
            except DeploymentError:
                pass
            raise _gateway_error() from None


def _create_production_gateway(
    *,
    template: Path,
    runner: Any,
    root: Path | None = None,
    controller_pid: int | None = None,
) -> _OperationalGateway:
    selected_root = template.parents[2] if root is None else root
    parts = _create_gateway_fail_closed_authority_parts()
    inspector = _ExactListenerInspector(
        parts.listener_proof_mint,
        token=_OPERATIONAL_TOKEN,
        runner=runner,
        root=_canonical_absolute(selected_root),
    )
    return _OperationalGateway(
        Gateway(template=template, runner=runner),
        token=_OPERATIONAL_TOKEN,
        root=selected_root,
        controller_pid=controller_pid,
        challenge_issuer=parts.challenge_issuer,
        evidence_issuer=parts.evidence_issuer,
        listener_inspector=inspector,
    )
