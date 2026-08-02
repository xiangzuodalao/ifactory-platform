"""Pinned, receipt-verified CMMS host toolchain installation."""

from __future__ import annotations

import hashlib
import inspect
import os
import posixpath
import re
import secrets
import selectors
import shutil
import signal
import stat
import subprocess
import tarfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

from .errors import DeploymentError
from .process import (
    CommandResult,
    CommandRunner,
    CommandSpec,
    ProcessDeadlineExceeded,
    ProcessOutputLimitExceeded,
    _validate_spec,
)
from .records import (
    ActionCode,
    ClaimedApplyContext,
    InstalledToolchain,
    ToolchainReceipt,
    canonical_json_bytes,
    strict_canonical_json_loads,
)
from .secure_io import _rename_exclusive


_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}\Z")
_MANIFEST_MAX_BYTES = 256 * 1024
_TOOL_ORDER = ("temurin", "maven", "node")
_SIZE_CEILINGS = {
    "temurin": 256 * 1024 * 1024,
    "maven": 32 * 1024 * 1024,
    "node": 64 * 1024 * 1024,
}
_RECEIPT_NAME = ".ifactory-toolchain-receipt.json"
_CURL_PATH = Path("/usr/bin/curl")
_HEADER_MAX_BYTES = 64 * 1024
_MAX_REDIRECTS = 10
_REVIEWED_MANIFEST_SHA256 = (
    "66ffef0523e645773d4eeee15a3ad1922"
    "f0f612fb7d3458641f34aec3d00f035"
)
_MANIFEST_MINT_TOKEN = object()
_VERIFIED_BINDING_TOKEN = object()
_RUNNER_MEMBER_MISSING = object()


@dataclass(frozen=True)
class BoundedDownloadSpec:
    """A pipe-backed fetch into one already-opened destination descriptor."""

    command: CommandSpec = field(repr=False)
    destination_fd: int = field(repr=False)
    body_limit_bytes: int
    header_limit_bytes: int

    def __post_init__(self) -> None:
        try:
            if (
                type(self.command) is not CommandSpec
                or type(self.destination_fd) is not int
                or self.destination_fd < 0
                or type(self.body_limit_bytes) is not int
                or self.body_limit_bytes <= 0
                or type(self.header_limit_bytes) is not int
                or self.header_limit_bytes <= 0
                or self.header_limit_bytes > _HEADER_MAX_BYTES
                or self.command.input_bytes is not None
            ):
                raise _installation_error()
            metadata = os.fstat(self.destination_fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size != 0
            ):
                raise _installation_error()
        except DeploymentError:
            raise
        except OSError:
            raise _installation_error()

    @property
    def file_size_limit_bytes(self) -> int:
        return self.body_limit_bytes

    @property
    def argv(self) -> tuple[str, ...]:
        return self.command.argv

    @property
    def cwd(self) -> Path:
        return self.command.cwd

    @property
    def environment(self) -> Any:
        return self.command.environment

    @property
    def timeout_seconds(self) -> float:
        return self.command.timeout_seconds

    @property
    def stdout_limit(self) -> int:
        return self.command.stdout_limit

    @property
    def stderr_limit(self) -> int:
        return self.command.stderr_limit

    @property
    def safe_label(self) -> str:
        return self.command.safe_label


@dataclass(frozen=True)
class BoundedDownloadResult:
    headers: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.headers) is not bytes:
            raise _installation_error()


def _close_download_streams(process: subprocess.Popen[bytes]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _terminate_download(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass
    _close_download_streams(process)


def _close_selected_stream(
    selector: selectors.BaseSelector,
    stream: Any,
) -> None:
    try:
        selector.unregister(stream)
    except (KeyError, ValueError):
        pass
    try:
        stream.close()
    except OSError:
        pass


def _pwrite_all(fd: int, data: bytes, offset: int) -> None:
    written_total = 0
    while written_total < len(data):
        written = os.pwrite(
            fd,
            data[written_total:],
            offset + written_total,
        )
        if written <= 0:
            raise OSError
        written_total += written


def _collect_download_streams(
    process: subprocess.Popen[bytes],
    spec: BoundedDownloadSpec,
    *,
    deadline: float,
) -> bytes:
    if process.stdout is None or process.stderr is None:
        raise _installation_error()
    selector = selectors.DefaultSelector()
    headers = bytearray()
    body_size = 0
    streams = {
        process.stdout: "body",
        process.stderr: "headers",
    }
    try:
        for stream in streams:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            now = time.monotonic()
            if now >= deadline:
                raise ProcessDeadlineExceeded
            events = selector.select(deadline - now)
            if not events:
                raise ProcessDeadlineExceeded
            for key, _mask in events:
                stream = key.fileobj
                kind = streams[stream]
                current_size = body_size if kind == "body" else len(headers)
                limit = (
                    spec.body_limit_bytes
                    if kind == "body"
                    else spec.header_limit_bytes
                )
                try:
                    chunk = os.read(
                        stream.fileno(),
                        min(65_536, limit - current_size + 1),
                    )
                except BlockingIOError:
                    continue
                if not chunk:
                    _close_selected_stream(selector, stream)
                    continue
                if current_size + len(chunk) > limit:
                    raise ProcessOutputLimitExceeded
                if kind == "body":
                    _pwrite_all(spec.destination_fd, chunk, body_size)
                    body_size += len(chunk)
                else:
                    headers.extend(chunk)
        os.fsync(spec.destination_fd)
        return bytes(headers)
    finally:
        selector.close()


class BoundedToolchainRunner:
    """Runs probes normally and bounds fetch pipes before descriptor writes."""

    def __init__(self, command_runner: Any | None = None) -> None:
        runner = CommandRunner() if command_runner is None else command_runner
        if not callable(getattr(runner, "run", None)):
            raise _installation_error()
        self._command_runner = runner

    def run(self, spec: CommandSpec) -> CommandResult:
        return self._command_runner.run(spec)

    def download(self, spec: BoundedDownloadSpec) -> BoundedDownloadResult:
        if type(spec) is not BoundedDownloadSpec:
            raise _installation_error()
        _validate_spec(spec.command)
        try:
            process = subprocess.Popen(
                spec.argv,
                cwd=spec.cwd,
                env=dict(spec.environment),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            raise _installation_error() from None
        deadline = time.monotonic() + spec.timeout_seconds
        try:
            headers = _collect_download_streams(
                process,
                spec,
                deadline=deadline,
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProcessDeadlineExceeded
            returncode = process.wait(timeout=remaining)
        except (
            ProcessDeadlineExceeded,
            ProcessOutputLimitExceeded,
            subprocess.TimeoutExpired,
            OSError,
            ValueError,
        ):
            _terminate_download(process)
            raise DeploymentError(
                "CMMS-E005",
                f"{spec.safe_label} did not complete safely",
                5,
            ) from None
        _close_download_streams(process)
        if returncode != 0:
            raise DeploymentError(
                "CMMS-E004",
                f"{spec.safe_label} failed",
                4,
            )
        return BoundedDownloadResult(headers)


def _manifest_error() -> DeploymentError:
    return DeploymentError("CMMS-E030", "invalid toolchain manifest", 30)


def _archive_error() -> DeploymentError:
    return DeploymentError("CMMS-E031", "toolchain archive is unsafe", 31)


def _installation_error() -> DeploymentError:
    return DeploymentError("CMMS-E032", "toolchain installation is not verified", 32)


def _exact_dict(value: object, keys: tuple[str, ...]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != set(keys) or len(value) != len(keys):
        raise _manifest_error()
    return value


def _string(value: object) -> str:
    if type(value) is not str or not value or "\x00" in value:
        raise _manifest_error()
    return value


def _positive_int(value: object, *, maximum: int | None = None) -> int:
    if (
        type(value) is not int
        or value <= 0
        or (maximum is not None and value > maximum)
    ):
        raise _manifest_error()
    return value


def _https_url(value: object) -> str:
    url = _string(value)
    parsed = urlsplit(url)
    decoded = unquote(url).casefold()
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or _selects_latest(decoded)
    ):
        raise _manifest_error()
    return url


def _selects_latest(value: str) -> bool:
    return re.search(r"(?:^|[^a-z0-9])latest(?:[^a-z0-9]|$)", value) is not None


def _safe_relative(value: object, *, one_component: bool = False) -> str:
    text = _string(value)
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or (one_component and len(path.parts) != 1)
    ):
        raise _manifest_error()
    return text


@dataclass(frozen=True)
class VersionProbe:
    executable: str
    arguments: tuple[str, ...]
    contains: str

    def __post_init__(self) -> None:
        _safe_relative(self.executable)
        if not self.executable.startswith("bin/"):
            raise _manifest_error()
        if type(self.arguments) is not tuple or not self.arguments:
            raise _manifest_error()
        for argument in self.arguments:
            text = _string(argument)
            if not text.isascii() or not text.isprintable():
                raise _manifest_error()
        expected = _string(self.contains)
        if not expected.isascii() or not expected.isprintable():
            raise _manifest_error()


@dataclass(frozen=True)
class ToolchainDefinition:
    name: str
    version: str
    url: str
    sha256: str
    archive_kind: str
    top_level_directory: str
    strip_components: int
    max_bytes: int
    probes: tuple[VersionProbe, ...]

    def __post_init__(self) -> None:
        if self.name not in _TOOL_ORDER:
            raise _manifest_error()
        if _VERSION.fullmatch(self.version) is None:
            raise _manifest_error()
        _https_url(self.url)
        if _HEX64.fullmatch(self.sha256) is None:
            raise _manifest_error()
        if self.archive_kind not in {"tar.gz", "tar.xz"}:
            raise _manifest_error()
        _safe_relative(self.top_level_directory, one_component=True)
        if type(self.strip_components) is not int or self.strip_components != 1:
            raise _manifest_error()
        _positive_int(self.max_bytes, maximum=_SIZE_CEILINGS[self.name])
        if type(self.probes) is not tuple or not self.probes or any(
            type(probe) is not VersionProbe for probe in self.probes
        ):
            raise _manifest_error()
        executables = tuple(probe.executable for probe in self.probes)
        expected = {
            "temurin": ("bin/java",),
            "maven": ("bin/mvn",),
            "node": ("bin/node", "bin/npm"),
        }[self.name]
        if executables != expected:
            raise _manifest_error()


@dataclass(frozen=True, init=False)
class ToolchainManifest:
    schema_version: int
    platform: str
    tools: tuple[ToolchainDefinition, ...]
    sha256: str = field(repr=False)

    def __init__(
        self,
        token: object,
        /,
        *,
        schema_version: int,
        platform: str,
        tools: tuple[ToolchainDefinition, ...],
        sha256: str,
    ) -> None:
        if token is not _MANIFEST_MINT_TOKEN:
            raise _manifest_error()
        if type(schema_version) is not int or schema_version != 1:
            raise _manifest_error()
        if platform != "linux/amd64":
            raise _manifest_error()
        if type(tools) is not tuple or any(
            type(tool) is not ToolchainDefinition for tool in tools
        ):
            raise _manifest_error()
        if tuple(tool.name for tool in tools) != _TOOL_ORDER:
            raise _manifest_error()
        if _HEX64.fullmatch(sha256) is None:
            raise _manifest_error()
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "platform", platform)
        object.__setattr__(self, "tools", tools)
        object.__setattr__(self, "sha256", sha256)

    @classmethod
    def load(cls, path: Path) -> ToolchainManifest:
        return cls._load(path)

    @classmethod
    def _load(
        cls,
        path: Path,
    ) -> ToolchainManifest:
        try:
            if not isinstance(path, Path):
                raise _manifest_error()
            before = path.lstat()
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_size <= 0
                or before.st_size > _MANIFEST_MAX_BYTES
            ):
                raise _manifest_error()
            data = _read_stable_regular_file(
                path,
                before,
                maximum=_MANIFEST_MAX_BYTES,
            )
            value = strict_canonical_json_loads(
                data,
                max_bytes=_MANIFEST_MAX_BYTES,
                max_depth=6,
            )
            root = _exact_dict(value, ("schema_version", "platform", "tools"))
            tools_value = root["tools"]
            if type(tools_value) is not list:
                raise _manifest_error()
            tools: list[ToolchainDefinition] = []
            for item in tools_value:
                row = _exact_dict(
                    item,
                    (
                        "name",
                        "version",
                        "url",
                        "sha256",
                        "archive_kind",
                        "top_level_directory",
                        "strip_components",
                        "max_bytes",
                        "probes",
                    ),
                )
                probes_value = row["probes"]
                if type(probes_value) is not list:
                    raise _manifest_error()
                probes: list[VersionProbe] = []
                for probe_value in probes_value:
                    probe = _exact_dict(
                        probe_value,
                        ("executable", "arguments", "contains"),
                    )
                    arguments = probe["arguments"]
                    if type(arguments) is not list:
                        raise _manifest_error()
                    probes.append(
                        VersionProbe(
                            executable=_string(probe["executable"]),
                            arguments=tuple(_string(value) for value in arguments),
                            contains=_string(probe["contains"]),
                        )
                    )
                tools.append(
                    ToolchainDefinition(
                        name=_string(row["name"]),
                        version=_string(row["version"]),
                        url=_string(row["url"]),
                        sha256=_string(row["sha256"]),
                        archive_kind=_string(row["archive_kind"]),
                        top_level_directory=_string(row["top_level_directory"]),
                        strip_components=row["strip_components"],
                        max_bytes=row["max_bytes"],
                        probes=tuple(probes),
                    )
                )
            digest = hashlib.sha256(data).hexdigest()
            if digest != _REVIEWED_MANIFEST_SHA256:
                raise _manifest_error()
            return cls(
                _MANIFEST_MINT_TOKEN,
                schema_version=root["schema_version"],
                platform=_string(root["platform"]),
                tools=tuple(tools),
                sha256=digest,
            )
        except DeploymentError as error:
            if error.code == "CMMS-E030":
                raise
            raise _manifest_error() from None
        except (OSError, TypeError, ValueError, UnicodeError):
            raise _manifest_error() from None

    def require(self, name: str) -> ToolchainDefinition:
        if type(name) is not str:
            raise _manifest_error()
        for tool in self.tools:
            if tool.name == name:
                return tool
        raise _manifest_error()

@dataclass(frozen=True)
class ToolchainStatus:
    name: str
    version: str
    home: Path = field(repr=False)
    installed: bool
    ready: bool
    code: str


def checked_archive_target(root: Path, member_name: str) -> Path:
    member = PurePosixPath(member_name)
    if member.is_absolute() or ".." in member.parts:
        raise _archive_error()
    target = root.joinpath(*member.parts).resolve(strict=False)
    if not target.is_relative_to(root.resolve()):
        raise _archive_error()
    return target


def _canonical_absolute(path: Path) -> Path:
    try:
        canonical = Path(os.path.abspath(os.fspath(path)))
    except (OSError, TypeError, ValueError):
        raise _installation_error() from None
    if not isinstance(path, Path) or not canonical.is_absolute() or canonical != path:
        raise _installation_error()
    return canonical


def _require_private_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        raise _installation_error() from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise _installation_error()


def _ensure_private_child(parent: Path, name: str) -> Path:
    _require_private_directory(parent)
    path = parent / name
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError:
        raise _installation_error() from None
    _require_private_directory(path)
    return path


def _read_install_receipt(path: Path, expected: dict[str, Any]) -> bool:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size <= 0
            or metadata.st_size > 16 * 1024
        ):
            return False
        data = _read_stable_regular_file(path, metadata, maximum=16 * 1024)
        value = strict_canonical_json_loads(data, max_bytes=16 * 1024, max_depth=3)
        return value == expected
    except (DeploymentError, OSError):
        return False


def _write_install_receipt(path: Path, value: dict[str, Any]) -> None:
    data = canonical_json_bytes(value)
    fd = -1
    try:
        fd = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | os.O_CLOEXEC,
            0o600,
        )
        offset = 0
        while offset < len(data):
            written = os.write(fd, data[offset:])
            if written <= 0:
                raise _installation_error()
            offset += written
        os.fsync(fd)
    except DeploymentError:
        raise
    except OSError:
        raise _installation_error() from None
    finally:
        if fd >= 0:
            os.close(fd)


def _install_receipt_mapping(
    tool: ToolchainDefinition,
    manifest_sha256: str,
) -> dict[str, Any]:
    return {
        "archive_sha256": tool.sha256,
        "manifest_sha256": manifest_sha256,
        "name": tool.name,
        "record_type": "cmms-toolchain-installation",
        "schema_version": 1,
        "version": tool.version,
    }


def _bound_stat_tuple(value: os.stat_result) -> tuple[int, ...]:
    return tuple(getattr(value, field_name) for field_name in _BOUND_STAT_FIELDS)


@dataclass(frozen=True, init=False)
class VerifiedToolchainBinding:
    """Descriptor-backed evidence for one exact installed toolchain set."""

    runtime_root: Path
    manifest_sha256: str
    identities: tuple[tuple[object, ...], ...] = field(repr=False)

    def __init__(
        self,
        token: object,
        runtime_root: Path,
        manifest_sha256: str,
        identities: tuple[tuple[object, ...], ...],
    ) -> None:
        if token is not _VERIFIED_BINDING_TOKEN:
            raise _installation_error()
        object.__setattr__(self, "runtime_root", runtime_root)
        object.__setattr__(self, "manifest_sha256", manifest_sha256)
        object.__setattr__(self, "identities", identities)


def _read_bound_install_receipt(
    home_fd: int,
    expected: dict[str, Any],
) -> tuple[int, ...]:
    fd = -1
    try:
        fd = os.open(
            _RECEIPT_NAME,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=home_fd,
        )
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size <= 0
            or before.st_size > 16 * 1024
        ):
            raise _installation_error()
        data = bytearray()
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(65_536, remaining))
            if not chunk:
                raise _installation_error()
            data.extend(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise _installation_error()
        after = os.fstat(fd)
        path_after = os.stat(
            _RECEIPT_NAME,
            dir_fd=home_fd,
            follow_symlinks=False,
        )
        if (
            not _same_bound_stat(before, after)
            or not _same_bound_stat(before, path_after)
            or strict_canonical_json_loads(
                bytes(data),
                max_bytes=16 * 1024,
                max_depth=3,
            )
            != expected
        ):
            raise _installation_error()
        return _bound_stat_tuple(before)
    except DeploymentError:
        raise
    except (OSError, TypeError, ValueError):
        raise _installation_error() from None
    finally:
        if fd >= 0:
            os.close(fd)


def _require_bound_home_path(
    home: Path,
    home_fd: int,
    expected: os.stat_result,
) -> None:
    try:
        descriptor = os.fstat(home_fd)
        lexical = home.lstat()
        resolved_path = home.resolve(strict=True)
        resolved = resolved_path.stat()
        if (
            resolved_path != home
            or not _same_bound_stat(expected, descriptor)
            or not _same_bound_stat(expected, lexical)
            or not _same_bound_stat(expected, resolved)
        ):
            raise _installation_error()
    except DeploymentError:
        raise
    except (OSError, TypeError, ValueError):
        raise _installation_error() from None


@dataclass
class _OpenToolchainHome:
    tool: ToolchainDefinition
    path: Path
    fd: int = field(repr=False)
    snapshot: os.stat_result = field(repr=False)
    receipt_identity: tuple[int, ...] = field(repr=False)


@dataclass(frozen=True)
class _BoundToolchainProbe:
    home: _OpenToolchainHome = field(repr=False)
    probe: VersionProbe
    executable: Path
    resolved: Path
    lexical_snapshot: os.stat_result = field(repr=False)
    resolved_snapshot: os.stat_result = field(repr=False)


def _private_directory_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
    )


def _open_bound_private_directory(path: Path) -> tuple[int, os.stat_result]:
    fd = -1
    try:
        fd = os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        snapshot = os.fstat(fd)
        if (
            not stat.S_ISDIR(snapshot.st_mode)
            or snapshot.st_uid != os.getuid()
            or stat.S_IMODE(snapshot.st_mode) != 0o700
        ):
            raise _installation_error()
        _require_bound_home_path(path, fd, snapshot)
        result = (fd, snapshot)
        fd = -1
        return result
    except DeploymentError:
        raise
    except (OSError, TypeError, ValueError):
        raise _installation_error() from None
    finally:
        if fd >= 0:
            os.close(fd)


def _capture_bound_probe(
    home: _OpenToolchainHome,
    probe: VersionProbe,
) -> _BoundToolchainProbe:
    executable = home.path.joinpath(*PurePosixPath(probe.executable).parts)
    resolved = executable.resolve(strict=True)
    lexical = executable.lstat()
    resolved_metadata = resolved.stat()
    if (
        not resolved.is_relative_to(home.path)
        or not stat.S_ISREG(resolved_metadata.st_mode)
        or resolved_metadata.st_uid != os.getuid()
        or resolved_metadata.st_nlink != 1
        or not resolved_metadata.st_mode & stat.S_IXUSR
        or not (stat.S_ISREG(lexical.st_mode) or stat.S_ISLNK(lexical.st_mode))
        or (stat.S_ISREG(lexical.st_mode) and lexical.st_nlink != 1)
    ):
        raise _installation_error()
    return _BoundToolchainProbe(
        home,
        probe,
        executable,
        resolved,
        lexical,
        resolved_metadata,
    )


def _require_bound_probe(row: _BoundToolchainProbe) -> None:
    try:
        resolved = row.executable.resolve(strict=True)
        lexical = row.executable.lstat()
        resolved_metadata = resolved.stat()
        if (
            resolved != row.resolved
            or not _same_bound_stat(row.lexical_snapshot, lexical)
            or not _same_bound_stat(row.resolved_snapshot, resolved_metadata)
        ):
            raise _installation_error()
    except DeploymentError:
        raise
    except (OSError, TypeError, ValueError):
        raise _installation_error() from None


def _require_bound_toolchain_probe_set(
    home: _OpenToolchainHome,
    probes: tuple[_BoundToolchainProbe, ...],
    manifest_sha256: str,
) -> None:
    _require_bound_home_path(home.path, home.fd, home.snapshot)
    if home.receipt_identity != _read_bound_install_receipt(
        home.fd,
        _install_receipt_mapping(home.tool, manifest_sha256),
    ):
        raise _installation_error()
    for probe in probes:
        _require_bound_probe(probe)


def _require_complete_toolchain_binding(
    runtime_root: Path,
    runtime_fd: int,
    runtime_snapshot: os.stat_result,
    toolchains_root: Path,
    toolchains_fd: int,
    toolchains_snapshot: os.stat_result,
    homes: list[_OpenToolchainHome],
    probes: list[_BoundToolchainProbe],
    manifest_sha256: str,
) -> None:
    _require_bound_home_path(runtime_root, runtime_fd, runtime_snapshot)
    _require_bound_home_path(toolchains_root, toolchains_fd, toolchains_snapshot)
    for home in homes:
        _require_bound_home_path(home.path, home.fd, home.snapshot)
        if home.receipt_identity != _read_bound_install_receipt(
            home.fd,
            _install_receipt_mapping(home.tool, manifest_sha256),
        ):
            raise _installation_error()
    for probe in probes:
        _require_bound_probe(probe)


def verify_toolchain_receipt(
    manifest: ToolchainManifest,
    receipt: ToolchainReceipt,
    runner: Any,
    *,
    runtime_root: Path,
    expected: VerifiedToolchainBinding | None = None,
    reprobe: bool = True,
) -> VerifiedToolchainBinding:
    """Re-probe and descriptor-bind an installed production toolchain set."""

    if (
        type(manifest) is not ToolchainManifest
        or manifest.sha256 != _REVIEWED_MANIFEST_SHA256
        or type(receipt) is not ToolchainReceipt
        or not callable(getattr(runner, "run", None))
        or (expected is not None and type(expected) is not VerifiedToolchainBinding)
        or type(reprobe) is not bool
        or (not reprobe and expected is None)
    ):
        raise _installation_error()
    root = _canonical_absolute(runtime_root)
    try:
        if root.resolve(strict=True) != root:
            raise _installation_error()
    except OSError:
        raise _installation_error() from None
    toolchains_root = root / "toolchains"
    if receipt.manifest_sha256 != manifest.sha256:
        raise _installation_error()

    runtime_fd = toolchains_fd = -1
    runtime_snapshot: os.stat_result | None = None
    toolchains_snapshot: os.stat_result | None = None
    opened: list[_OpenToolchainHome] = []
    bound_probes: list[_BoundToolchainProbe] = []
    identities: list[tuple[object, ...]] = []
    try:
        runtime_fd, runtime_snapshot = _open_bound_private_directory(root)
        toolchains_fd, toolchains_snapshot = _open_bound_private_directory(
            toolchains_root
        )
        # Validate every exact disk receipt before executing the first probe.
        for installed, tool in zip(receipt.installations, manifest.tools, strict=True):
            home = toolchains_root / f"{tool.name}-{tool.version}"
            if (
                installed.name != tool.name
                or installed.version != tool.version
                or installed.archive_sha256 != tool.sha256
                or installed.home != home
            ):
                raise _installation_error()
            home_fd = os.open(
                home,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            try:
                home_snapshot = os.fstat(home_fd)
                if (
                    not stat.S_ISDIR(home_snapshot.st_mode)
                    or home_snapshot.st_uid != os.getuid()
                    or stat.S_IMODE(home_snapshot.st_mode) != 0o700
                ):
                    raise _installation_error()
                _require_bound_home_path(home, home_fd, home_snapshot)
                disk_receipt = _read_bound_install_receipt(
                    home_fd,
                    _install_receipt_mapping(tool, manifest.sha256),
                )
                opened.append(
                    _OpenToolchainHome(
                        tool,
                        home,
                        home_fd,
                        home_snapshot,
                        disk_receipt,
                    )
                )
                home_fd = -1
            finally:
                if home_fd >= 0:
                    os.close(home_fd)

        environment = {
            "JAVA_HOME": str(opened[0].path),
            "LC_ALL": "C",
            "PATH": os.pathsep.join(str(row.path / "bin") for row in opened)
            + os.pathsep
            + "/usr/bin:/bin",
        }
        # Bind every executable before the first probe. An early probe must not be
        # able to establish a new baseline for a later executable.
        for row in opened:
            for probe in row.tool.probes:
                bound_probes.append(_capture_bound_probe(row, probe))

        identities.append(
            ("runtime-root", *_private_directory_identity(runtime_snapshot))
        )
        identities.append(
            ("toolchains-root", *_bound_stat_tuple(toolchains_snapshot))
        )
        for row in opened:
            identities.append((row.tool.name, "home", *_bound_stat_tuple(row.snapshot)))
            identities.append((row.tool.name, "receipt", *row.receipt_identity))
        for bound_probe in bound_probes:
            identities.append(
                (
                    bound_probe.home.tool.name,
                    bound_probe.probe.executable,
                    "lexical",
                    *_bound_stat_tuple(bound_probe.lexical_snapshot),
                )
            )
            identities.append(
                (
                    bound_probe.home.tool.name,
                    bound_probe.probe.executable,
                    "resolved",
                    *_bound_stat_tuple(bound_probe.resolved_snapshot),
                )
            )
        binding = VerifiedToolchainBinding(
            _VERIFIED_BINDING_TOKEN,
            root,
            manifest.sha256,
            tuple(identities),
        )
        if expected is not None and binding != expected:
            raise _installation_error()

        _require_complete_toolchain_binding(
            root,
            runtime_fd,
            runtime_snapshot,
            toolchains_root,
            toolchains_fd,
            toolchains_snapshot,
            opened,
            bound_probes,
            manifest.sha256,
        )
        if not reprobe:
            return binding
        for bound_probe in bound_probes:
            row = bound_probe.home
            probe = bound_probe.probe
            probe_environment = {
                **environment,
                "JAVA_HOME": str(
                    row.path if row.tool.name == "temurin" else opened[0].path
                ),
            }
            _require_complete_toolchain_binding(
                root,
                runtime_fd,
                runtime_snapshot,
                toolchains_root,
                toolchains_fd,
                toolchains_snapshot,
                opened,
                bound_probes,
                manifest.sha256,
            )
            try:
                result = runner.run(
                    CommandSpec(
                        argv=(str(bound_probe.executable), *probe.arguments),
                        cwd=row.path,
                        environment=probe_environment,
                        timeout_seconds=20,
                        stdout_limit=64 * 1024,
                        stderr_limit=64 * 1024,
                        safe_label=f"toolchain-{row.tool.name}-version",
                    )
                )
            except DeploymentError:
                raise
            except Exception:
                raise _installation_error() from None
            _require_complete_toolchain_binding(
                root,
                runtime_fd,
                runtime_snapshot,
                toolchains_root,
                toolchains_fd,
                toolchains_snapshot,
                opened,
                bound_probes,
                manifest.sha256,
            )
            if (
                type(result) is not CommandResult
                or type(result.returncode) is not int
                or result.returncode != 0
                or type(result.stdout) is not str
                or type(result.stderr) is not str
                or probe.contains not in f"{result.stdout}\n{result.stderr}"
            ):
                raise _installation_error()

        _require_complete_toolchain_binding(
            root,
            runtime_fd,
            runtime_snapshot,
            toolchains_root,
            toolchains_fd,
            toolchains_snapshot,
            opened,
            bound_probes,
            manifest.sha256,
        )
        return binding
    except DeploymentError:
        raise
    except (OSError, TypeError, ValueError):
        raise _installation_error() from None
    finally:
        for row in opened:
            try:
                os.close(row.fd)
            except OSError:
                pass
        for fd in (toolchains_fd, runtime_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _safe_effective_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    decoded = unquote(value).casefold()
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
        and "\n" not in value
        and "\r" not in value
        and not _selects_latest(decoded)
    )


def _validate_redirect_chain(
    initial_url: str,
    header_bytes: bytes,
) -> str:
    try:
        if (
            type(header_bytes) is not bytes
            or not header_bytes
            or len(header_bytes) > _HEADER_MAX_BYTES
            or b"\x00" in header_bytes
            or not _safe_effective_url(initial_url)
        ):
            raise _installation_error()
        text = header_bytes.decode("ascii")
        if text.replace("\r\n", "").find("\n") >= 0:
            raise _installation_error()
        blocks = tuple(
            block for block in text.split("\r\n\r\n") if block
        )
        if not blocks or not text.endswith("\r\n\r\n"):
            raise _installation_error()
        current_url = initial_url
        redirect_count = 0
        for index, block in enumerate(blocks):
            lines = block.split("\r\n")
            match = re.fullmatch(r"HTTP/\S+\s+([0-9]{3})(?:\s+.*)?", lines[0])
            if match is None:
                raise _installation_error()
            status_code = int(match.group(1))
            locations: list[str] = []
            for line in lines[1:]:
                if not line or line[0].isspace() or ":" not in line:
                    raise _installation_error()
                name, value = line.split(":", 1)
                if (
                    not name
                    or not name.isascii()
                    or any(
                        not (character.isalnum() or character == "-")
                        for character in name
                    )
                ):
                    raise _installation_error()
                if name.casefold() == "location":
                    location = value.strip(" \t")
                    if not location:
                        raise _installation_error()
                    locations.append(location)
            is_final = index == len(blocks) - 1
            if not is_final:
                if status_code not in {301, 302, 303, 307, 308}:
                    raise _installation_error()
                if len(locations) != 1:
                    raise _installation_error()
                redirect_count += 1
                if redirect_count > _MAX_REDIRECTS:
                    raise _installation_error()
                current_url = urljoin(current_url, locations[0])
                if not _safe_effective_url(current_url):
                    raise _installation_error()
            else:
                if not 200 <= status_code < 300:
                    raise _installation_error()
                for location in locations:
                    if not _safe_effective_url(urljoin(current_url, location)):
                        raise _installation_error()
        return current_url
    except DeploymentError:
        raise
    except (TypeError, ValueError, UnicodeError):
        raise _installation_error() from None


_BOUND_STAT_FIELDS = (
    "st_dev",
    "st_ino",
    "st_mode",
    "st_uid",
    "st_nlink",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
)


def _same_bound_stat(left: os.stat_result, right: os.stat_result) -> bool:
    return all(
        getattr(left, field_name) == getattr(right, field_name)
        for field_name in _BOUND_STAT_FIELDS
    )


def _read_stable_regular_file(
    path: Path,
    before_path: os.stat_result,
    *,
    maximum: int,
) -> bytes:
    fd = -1
    try:
        fd = os.open(
            path,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
        )
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > maximum
            or not _same_bound_stat(before_path, before)
        ):
            raise OSError
        data = bytearray()
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(65_536, remaining))
            if not chunk:
                raise OSError
            data.extend(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise OSError
        after = os.fstat(fd)
        after_path = path.lstat()
        if (
            not _same_bound_stat(before, after)
            or not _same_bound_stat(before, after_path)
        ):
            raise OSError
        return bytes(data)
    finally:
        if fd >= 0:
            os.close(fd)


@dataclass
class _BoundFile:
    """An exclusive file kept descriptor-bound until its final consumer."""

    path: Path
    fd: int = field(repr=False)
    max_bytes: int
    identity: tuple[int, int] = field(repr=False)
    snapshot: os.stat_result | None = field(default=None, init=False, repr=False)
    closed: bool = field(default=False, init=False, repr=False)

    @classmethod
    def create(cls, path: Path, *, max_bytes: int) -> _BoundFile:
        fd = -1
        try:
            if (
                not isinstance(path, Path)
                or type(max_bytes) is not int
                or max_bytes <= 0
            ):
                raise _installation_error()
            fd = os.open(
                path,
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | os.O_CLOEXEC,
                0o600,
            )
            metadata = os.fstat(fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size != 0
            ):
                raise _installation_error()
            return cls(
                path,
                fd,
                max_bytes,
                (metadata.st_dev, metadata.st_ino),
            )
        except DeploymentError:
            if fd >= 0:
                os.close(fd)
            raise
        except OSError:
            if fd >= 0:
                os.close(fd)
            raise _installation_error() from None

    def capture_written(self) -> None:
        try:
            before = os.fstat(self.fd)
            lexical = self.path.lstat()
            after = os.fstat(self.fd)
            if (
                self.closed
                or (after.st_dev, after.st_ino) != self.identity
                or not _same_bound_stat(before, after)
                or not _same_bound_stat(lexical, after)
                or not stat.S_ISREG(after.st_mode)
                or after.st_uid != os.getuid()
                or after.st_nlink != 1
                or stat.S_IMODE(after.st_mode) != 0o600
                or after.st_size <= 0
                or after.st_size > self.max_bytes
            ):
                raise _installation_error()
            self.snapshot = after
        except DeploymentError:
            raise
        except OSError:
            raise _installation_error() from None

    def require_bound(self) -> os.stat_result:
        try:
            before = os.fstat(self.fd)
            lexical = self.path.lstat()
            after = os.fstat(self.fd)
            if (
                self.closed
                or self.snapshot is None
                or not _same_bound_stat(before, after)
                or not _same_bound_stat(self.snapshot, after)
                or not _same_bound_stat(lexical, after)
            ):
                raise _installation_error()
            return after
        except DeploymentError:
            raise
        except OSError:
            raise _installation_error() from None

    def read_bytes(self) -> bytes:
        expected = self.require_bound()
        chunks: list[bytes] = []
        remaining = self.max_bytes + 1
        try:
            os.lseek(self.fd, 0, os.SEEK_SET)
            while remaining:
                chunk = os.read(self.fd, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) != expected.st_size or len(data) > self.max_bytes:
                raise _installation_error()
            self.require_bound()
            return data
        except DeploymentError:
            raise
        except OSError:
            raise _installation_error() from None

    def close(self) -> None:
        if self.closed:
            return
        try:
            try:
                descriptor = os.fstat(self.fd)
                lexical = self.path.lstat()
                if (
                    (descriptor.st_dev, descriptor.st_ino) == self.identity
                    and (lexical.st_dev, lexical.st_ino) == self.identity
                ):
                    self.path.unlink()
            except OSError:
                pass
        finally:
            os.close(self.fd)
            self.closed = True


def _hash_bound_archive(bound: _BoundFile, expected_sha256: str) -> None:
    expected = bound.require_bound()
    digest = hashlib.sha256()
    total = 0
    try:
        os.lseek(bound.fd, 0, os.SEEK_SET)
        while chunk := os.read(bound.fd, 65_536):
            total += len(chunk)
            if total > bound.max_bytes:
                raise _installation_error()
            digest.update(chunk)
        if total != expected.st_size or digest.hexdigest() != expected_sha256:
            raise _installation_error()
        bound.require_bound()
    except DeploymentError:
        raise
    except OSError:
        raise _installation_error() from None


def _link_target(
    extraction_root: Path,
    stripped_member: PurePosixPath,
    linkname: str,
    *,
    hardlink: bool,
    top_level: str,
) -> Path:
    link = PurePosixPath(linkname)
    if link.is_absolute() or not link.parts:
        raise _archive_error()
    if hardlink:
        if link.parts[0] != top_level or len(link.parts) < 2:
            raise _archive_error()
        candidate = PurePosixPath(*link.parts[1:])
    else:
        normalized = posixpath.normpath(
            str(stripped_member.parent.joinpath(link))
        )
        candidate = PurePosixPath(normalized)
    return checked_archive_target(extraction_root, str(candidate))


@dataclass(frozen=True)
class _ArchiveMember:
    info: tarfile.TarInfo
    target: Path
    link_target: Path | None


def _scan_archive(
    archive: tarfile.TarFile,
    *,
    extraction_root: Path,
    tool: ToolchainDefinition,
) -> tuple[_ArchiveMember, ...]:
    rows: list[_ArchiveMember] = []
    targets: set[Path] = set()
    regular_targets: set[Path] = set()
    symlink_targets: set[Path] = set()
    total_size = 0
    root_directory_seen = False
    try:
        members = archive.getmembers()
    except (OSError, tarfile.TarError):
        raise _archive_error() from None
    if not members:
        raise _archive_error()
    for info in members:
        raw = PurePosixPath(info.name)
        if (
            raw.is_absolute()
            or ".." in raw.parts
            or not raw.parts
            or raw.parts[0] != tool.top_level_directory
        ):
            raise _archive_error()
        stripped = PurePosixPath(*raw.parts[tool.strip_components :])
        if not stripped.parts:
            if not info.isdir() or root_directory_seen:
                raise _archive_error()
            root_directory_seen = True
            continue
        target = checked_archive_target(extraction_root, str(stripped))
        if target in targets:
            raise _archive_error()
        targets.add(target)
        if not (info.isdir() or info.isreg() or info.issym() or info.islnk()):
            raise _archive_error()
        if info.size < 0:
            raise _archive_error()
        total_size += info.size
        if total_size > tool.max_bytes * 4:
            raise _archive_error()
        link_target: Path | None = None
        if info.issym() or info.islnk():
            link_target = _link_target(
                extraction_root,
                stripped,
                info.linkname,
                hardlink=info.islnk(),
                top_level=tool.top_level_directory,
            )
        if info.isreg():
            regular_targets.add(target)
        if info.issym():
            symlink_targets.add(target)
        rows.append(_ArchiveMember(info, target, link_target))
    for row in rows:
        parent = row.target.parent
        while parent != extraction_root:
            if parent in symlink_targets:
                raise _archive_error()
            parent = parent.parent
        if row.info.islnk() and row.link_target not in regular_targets:
            raise _archive_error()
    return tuple(rows)


def _extract_archive(
    archive_file: _BoundFile,
    extraction_root: Path,
    tool: ToolchainDefinition,
) -> None:
    mode = "r:gz" if tool.archive_kind == "tar.gz" else "r:xz"
    duplicate_fd = -1
    try:
        archive_file.require_bound()
        os.lseek(archive_file.fd, 0, os.SEEK_SET)
        duplicate_fd = os.dup(archive_file.fd)
        stream = os.fdopen(duplicate_fd, "rb")
        duplicate_fd = -1
        with stream:
            with tarfile.open(fileobj=stream, mode=mode) as archive:
                rows = _scan_archive(
                    archive,
                    extraction_root=extraction_root,
                    tool=tool,
                )
                for row in sorted(
                    (item for item in rows if item.info.isdir()),
                    key=lambda item: len(item.target.parts),
                ):
                    row.target.mkdir(
                        mode=row.info.mode & 0o777,
                        parents=True,
                        exist_ok=False,
                    )
                for row in (item for item in rows if item.info.isreg()):
                    row.target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                    source = archive.extractfile(row.info)
                    if source is None:
                        raise _archive_error()
                    fd = -1
                    try:
                        fd = os.open(
                            row.target,
                            os.O_WRONLY
                            | os.O_CREAT
                            | os.O_EXCL
                            | os.O_NOFOLLOW
                            | os.O_CLOEXEC,
                            row.info.mode & 0o777,
                        )
                        remaining = row.info.size
                        while remaining:
                            chunk = source.read(min(65_536, remaining))
                            if not chunk:
                                raise _archive_error()
                            offset = 0
                            while offset < len(chunk):
                                written = os.write(fd, chunk[offset:])
                                if written <= 0:
                                    raise _archive_error()
                                offset += written
                            remaining -= len(chunk)
                        if source.read(1):
                            raise _archive_error()
                        os.fsync(fd)
                    finally:
                        try:
                            source.close()
                        finally:
                            if fd >= 0:
                                os.close(fd)
                for row in (item for item in rows if item.info.islnk()):
                    assert row.link_target is not None
                    os.link(row.link_target, row.target, follow_symlinks=False)
                for row in (item for item in rows if item.info.issym()):
                    os.symlink(row.info.linkname, row.target)
        archive_file.require_bound()
    except DeploymentError:
        raise
    except (OSError, tarfile.TarError, EOFError, ValueError):
        raise _archive_error() from None
    finally:
        if duplicate_fd >= 0:
            try:
                os.close(duplicate_fd)
            except OSError:
                pass


def _declares_runner_member(runner: object, name: str) -> bool:
    try:
        member = inspect.getattr_static(runner, name, _RUNNER_MEMBER_MISSING)
        if member is _RUNNER_MEMBER_MISSING:
            return False
        if callable(member):
            return True
        return inspect.getattr_static(
            type(member),
            "__get__",
            _RUNNER_MEMBER_MISSING,
        ) is not _RUNNER_MEMBER_MISSING
    except (AttributeError, TypeError, ValueError):
        return False


class _ResolvedToolchainRunner:
    """Fixed runner callables with optional live-action guards."""

    def __init__(
        self,
        run: Callable[[CommandSpec], object],
        download: Callable[[BoundedDownloadSpec], object] | None,
        context: ClaimedApplyContext | None,
    ) -> None:
        self._run = run
        self._download = download
        self._context = context

    def _require_action(self) -> None:
        if self._context is not None:
            self._context.require_action(ActionCode.TOOLCHAIN_INSTALL)

    def run(self, spec: CommandSpec) -> object:
        self._require_action()
        try:
            result = self._run(spec)
        except Exception:
            raise _installation_error() from None
        self._require_action()
        return result

    def download(self, spec: BoundedDownloadSpec) -> object:
        if self._download is None:
            raise _installation_error()
        self._require_action()
        try:
            result = self._download(spec)
        except Exception:
            raise _installation_error() from None
        self._require_action()
        return result


def _resolve_toolchain_runner(
    runner: object,
    *,
    context: ClaimedApplyContext | None,
    require_download: bool,
) -> _ResolvedToolchainRunner:
    def resolve(name: str) -> Callable[[Any], object]:
        if context is not None:
            context.require_action(ActionCode.TOOLCHAIN_INSTALL)
        try:
            member = getattr(runner, name)
            if not callable(member):
                raise TypeError("runner member is not callable")
        except Exception:
            raise _installation_error() from None
        if context is not None:
            context.require_action(ActionCode.TOOLCHAIN_INSTALL)
        return member

    run = resolve("run")
    download = resolve("download") if require_download else None
    return _ResolvedToolchainRunner(run, download, context)


class ToolchainInstaller:
    def __init__(
        self,
        manifest: ToolchainManifest,
        runner: Any,
        *,
        runtime_root: Path,
        curl_path: Path = _CURL_PATH,
    ) -> None:
        if (
            type(manifest) is not ToolchainManifest
            or manifest.sha256 != _REVIEWED_MANIFEST_SHA256
            or not _declares_runner_member(runner, "run")
            or not _declares_runner_member(runner, "download")
        ):
            raise _installation_error()
        self._manifest = manifest
        self._runner = runner
        self._runtime_root = _canonical_absolute(runtime_root)
        self._curl_path = _canonical_absolute(curl_path)
        if self._curl_path != _CURL_PATH:
            raise _installation_error()

    def _home(self, runtime_root: Path, tool: ToolchainDefinition) -> Path:
        return runtime_root / "toolchains" / f"{tool.name}-{tool.version}"

    def _environment(
        self,
        runtime_root: Path,
        *,
        java_home: Path,
        current_home: Path,
    ) -> dict[str, str]:
        homes = [current_home]
        homes.extend(
            home
            for name in _TOOL_ORDER
            if (home := self._home(runtime_root, self._manifest.require(name)))
            != current_home
        )
        return {
            "JAVA_HOME": str(java_home),
            "LC_ALL": "C",
            "PATH": os.pathsep.join(str(home / "bin") for home in homes)
            + os.pathsep
            + "/usr/bin:/bin",
        }

    def _probe(
        self,
        runtime_root: Path,
        tool: ToolchainDefinition,
        home: Path,
        runner: _ResolvedToolchainRunner,
    ) -> bool:
        home_fd = -1
        try:
            try:
                home_fd, home_snapshot = _open_bound_private_directory(home)
                receipt_identity = _read_bound_install_receipt(
                    home_fd,
                    _install_receipt_mapping(tool, self._manifest.sha256),
                )
                open_home = _OpenToolchainHome(
                    tool,
                    home,
                    home_fd,
                    home_snapshot,
                    receipt_identity,
                )
                bound_probes = tuple(
                    _capture_bound_probe(open_home, probe)
                    for probe in tool.probes
                )
                _require_bound_toolchain_probe_set(
                    open_home,
                    bound_probes,
                    self._manifest.sha256,
                )
            except DeploymentError:
                return False
            except (OSError, TypeError, ValueError):
                return False
            java_home = (
                home
                if tool.name == "temurin"
                else self._home(runtime_root, self._manifest.require("temurin"))
            )
            for bound_probe in bound_probes:
                probe = bound_probe.probe
                _require_bound_toolchain_probe_set(
                    open_home,
                    bound_probes,
                    self._manifest.sha256,
                )
                result = runner.run(
                    CommandSpec(
                        argv=(str(bound_probe.executable), *probe.arguments),
                        cwd=home,
                        environment=self._environment(
                            runtime_root,
                            java_home=java_home,
                            current_home=home,
                        ),
                        timeout_seconds=20,
                        stdout_limit=64 * 1024,
                        stderr_limit=64 * 1024,
                        safe_label=f"toolchain-{tool.name}-version",
                    )
                )
                _require_bound_toolchain_probe_set(
                    open_home,
                    bound_probes,
                    self._manifest.sha256,
                )
                if (
                    type(result) is not CommandResult
                    or type(result.returncode) is not int
                    or result.returncode != 0
                    or type(result.stdout) is not str
                    or type(result.stderr) is not str
                    or probe.contains not in f"{result.stdout}\n{result.stderr}"
                ):
                    return False
            _require_bound_toolchain_probe_set(
                open_home,
                bound_probes,
                self._manifest.sha256,
            )
            return True
        finally:
            if home_fd >= 0:
                try:
                    os.close(home_fd)
                except OSError:
                    pass

    def plan_status(self, runtime_root: Path) -> tuple[ToolchainStatus, ...]:
        runner = _resolve_toolchain_runner(
            self._runner,
            context=None,
            require_download=False,
        )
        return self._plan_status(runtime_root, runner, context=None)

    def _plan_status(
        self,
        runtime_root: Path,
        runner: _ResolvedToolchainRunner,
        *,
        context: ClaimedApplyContext | None,
    ) -> tuple[ToolchainStatus, ...]:
        def require_action() -> None:
            if context is not None:
                context.require_action(ActionCode.TOOLCHAIN_INSTALL)

        require_action()
        root = _canonical_absolute(runtime_root)
        _require_private_directory(root)
        require_action()
        toolchains_root = root / "toolchains"
        try:
            toolchains_root.lstat()
        except FileNotFoundError:
            result = tuple(
                ToolchainStatus(
                    tool.name,
                    tool.version,
                    self._home(root, tool),
                    False,
                    False,
                    "MISSING",
                )
                for tool in self._manifest.tools
            )
            require_action()
            return result
        except OSError:
            raise _installation_error() from None
        _require_private_directory(toolchains_root)
        require_action()
        statuses: list[ToolchainStatus] = []
        for tool in self._manifest.tools:
            require_action()
            home = self._home(root, tool)
            try:
                metadata = home.lstat()
            except FileNotFoundError:
                statuses.append(
                    ToolchainStatus(
                        tool.name,
                        tool.version,
                        home,
                        False,
                        False,
                        "MISSING",
                    )
                )
                require_action()
                continue
            except OSError:
                raise _installation_error() from None
            installed = True
            structurally_safe = (
                stat.S_ISDIR(metadata.st_mode)
                and metadata.st_uid == os.getuid()
                and stat.S_IMODE(metadata.st_mode) == 0o700
            )
            receipt_ok = structurally_safe and _read_install_receipt(
                home / _RECEIPT_NAME,
                _install_receipt_mapping(tool, self._manifest.sha256),
            )
            require_action()
            ready = receipt_ok and self._probe(root, tool, home, runner)
            require_action()
            statuses.append(
                ToolchainStatus(
                    tool.name,
                    tool.version,
                    home,
                    installed,
                    ready,
                    "READY" if ready else "MISMATCH",
                )
            )
        result = tuple(statuses)
        require_action()
        return result

    def _download(
        self,
        staging: Path,
        tool: ToolchainDefinition,
        runner: _ResolvedToolchainRunner,
        context: ClaimedApplyContext,
    ) -> _BoundFile:
        context.require_action(ActionCode.TOOLCHAIN_INSTALL)
        nonce = secrets.token_hex(16)
        keep = False
        archive_file = _BoundFile.create(
            staging / f".{tool.name}-{nonce}.download",
            max_bytes=tool.max_bytes,
        )
        try:
            context.require_action(ActionCode.TOOLCHAIN_INSTALL)
            result = runner.download(
                BoundedDownloadSpec(
                    command=CommandSpec(
                        argv=(
                            str(self._curl_path),
                            "--disable",
                            "--proto",
                            "=https",
                            "--proto-redir",
                            "=https",
                            "--tlsv1.2",
                            "--location",
                            "--max-redirs",
                            str(_MAX_REDIRECTS),
                            "--fail",
                            "--silent",
                            "--show-error",
                            "--max-filesize",
                            str(tool.max_bytes),
                            "--dump-header",
                            "/proc/self/fd/2",
                            "--output",
                            "/proc/self/fd/1",
                            tool.url,
                        ),
                        cwd=staging,
                        environment={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
                        timeout_seconds=900,
                        stdout_limit=tool.max_bytes,
                        stderr_limit=_HEADER_MAX_BYTES,
                        safe_label=f"toolchain-{tool.name}-download",
                    ),
                    destination_fd=archive_file.fd,
                    body_limit_bytes=tool.max_bytes,
                    header_limit_bytes=_HEADER_MAX_BYTES,
                )
            )
            context.require_action(ActionCode.TOOLCHAIN_INSTALL)
            if type(result) is not BoundedDownloadResult:
                raise _installation_error()
            archive_file.capture_written()
            _validate_redirect_chain(tool.url, result.headers)
            _hash_bound_archive(archive_file, tool.sha256)
            keep = True
            return archive_file
        except DeploymentError:
            raise
        except (OSError, TypeError, ValueError):
            raise _installation_error() from None
        finally:
            if not keep:
                archive_file.close()

    def _install_one(
        self,
        runtime_root: Path,
        toolchains_root: Path,
        staging: Path,
        tool: ToolchainDefinition,
        runner: _ResolvedToolchainRunner,
        context: ClaimedApplyContext,
    ) -> InstalledToolchain:
        context.require_action(ActionCode.TOOLCHAIN_INSTALL)
        temporary = toolchains_root / f".{tool.name}-{secrets.token_hex(16)}.tmp"
        published = False
        archive_file = self._download(staging, tool, runner, context)
        try:
            context.require_action(ActionCode.TOOLCHAIN_INSTALL)
            temporary.mkdir(mode=0o700)
            _require_private_directory(temporary)
            context.require_action(ActionCode.TOOLCHAIN_INSTALL)
            _extract_archive(archive_file, temporary, tool)
            context.require_action(ActionCode.TOOLCHAIN_INSTALL)
            _write_install_receipt(
                temporary / _RECEIPT_NAME,
                _install_receipt_mapping(tool, self._manifest.sha256),
            )
            context.require_action(ActionCode.TOOLCHAIN_INSTALL)
            if not self._probe(runtime_root, tool, temporary, runner):
                raise _installation_error()
            context.require_action(ActionCode.TOOLCHAIN_INSTALL)
            parent_fd = os.open(
                toolchains_root,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            try:
                _rename_exclusive(
                    temporary.name,
                    f"{tool.name}-{tool.version}",
                    directory_fd=parent_fd,
                )
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
            context.require_action(ActionCode.TOOLCHAIN_INSTALL)
            published = True
            home = self._home(runtime_root, tool)
            return InstalledToolchain(
                tool.name,
                tool.version,
                tool.sha256,
                home,
            )
        except DeploymentError:
            raise
        except OSError:
            raise _installation_error() from None
        finally:
            archive_file.close()
            if not published and temporary.exists():
                shutil.rmtree(temporary)

    def install_all(self, context: ClaimedApplyContext) -> ToolchainReceipt:
        if type(context) is not ClaimedApplyContext:
            raise _installation_error()
        context.require_action(ActionCode.TOOLCHAIN_INSTALL)
        if context.plan.snapshot.toolchain_manifest_sha256 != self._manifest.sha256:
            raise _installation_error()
        runner = _resolve_toolchain_runner(
            self._runner,
            context=context,
            require_download=True,
        )
        context.require_action(ActionCode.TOOLCHAIN_INSTALL)
        _require_private_directory(self._runtime_root)
        context.require_action(ActionCode.TOOLCHAIN_INSTALL)
        staging = _ensure_private_child(self._runtime_root, "cmms-staging")
        context.require_action(ActionCode.TOOLCHAIN_INSTALL)
        toolchains_root = _ensure_private_child(self._runtime_root, "toolchains")
        context.require_action(ActionCode.TOOLCHAIN_INSTALL)
        statuses = self._plan_status(
            self._runtime_root,
            runner,
            context=context,
        )
        context.require_action(ActionCode.TOOLCHAIN_INSTALL)
        installations: list[InstalledToolchain] = []
        for tool, status in zip(self._manifest.tools, statuses, strict=True):
            context.require_action(ActionCode.TOOLCHAIN_INSTALL)
            if status.installed:
                if not status.ready:
                    raise _installation_error()
                installations.append(
                    InstalledToolchain(
                        tool.name,
                        tool.version,
                        tool.sha256,
                        status.home,
                    )
                )
            else:
                installations.append(
                    self._install_one(
                        self._runtime_root,
                        toolchains_root,
                        staging,
                        tool,
                        runner,
                        context,
                    )
                )
            context.require_action(ActionCode.TOOLCHAIN_INSTALL)
        receipt = ToolchainReceipt(self._manifest.sha256, tuple(installations))
        context.require_action(ActionCode.TOOLCHAIN_INSTALL)
        binding = verify_toolchain_receipt(
            self._manifest,
            receipt,
            runner,
            runtime_root=self._runtime_root,
        )
        context.require_action(ActionCode.TOOLCHAIN_INSTALL)
        verify_toolchain_receipt(
            self._manifest,
            receipt,
            runner,
            runtime_root=self._runtime_root,
            expected=binding,
            reprobe=False,
        )
        context.require_action(ActionCode.TOOLCHAIN_INSTALL)
        return receipt


__all__ = [
    "BoundedDownloadResult",
    "BoundedDownloadSpec",
    "BoundedToolchainRunner",
    "ToolchainDefinition",
    "ToolchainInstaller",
    "ToolchainManifest",
    "ToolchainStatus",
    "VerifiedToolchainBinding",
    "VersionProbe",
    "checked_archive_target",
    "verify_toolchain_receipt",
]
