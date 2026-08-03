"""Fail-closed control of the isolated CMMS Compose state stack."""

from __future__ import annotations

import fcntl
import hashlib
import ipaddress
import json
import os
import re
import secrets
import stat
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping

from .config import RuntimeConfig
from .errors import DeploymentError
from .gateway import Gateway
from .process import CommandResult, CommandSpec
from .records import (
    ActionCode,
    ActionTargetKind,
    ClaimedApplyContext,
    GatewayMode,
    PlannedAction,
)
from .secure_io import RuntimePathPolicy, open_verified_parent


_DOCKER = "/usr/bin/docker"
_IP = "/usr/sbin/ip"
_DOCKER_HOST = "unix:///var/run/docker.sock"
_PROJECT = "ifactory-cmms-dev"
_COMPOSE_TARGET = "compose:ifactory-cmms-dev"
_SERVICES = ("postgres", "minio", "nginx")
_STOP_SERVICES = ("nginx", "minio", "postgres")
_PLATFORM = "linux/amd64"
_PATH = "/usr/bin:/bin"
_MAX_OUTPUT_BYTES = 64 * 1024
_MAX_RUNTIME_FILE_BYTES = 64 * 1024
_MAX_COMPOSE_FILE_BYTES = 64 * 1024
_MAX_NGINX_CONFIG_BYTES = 256 * 1024
_REVIEWED_COMPOSE_SHA256 = (
    "2579565873578252f528c906c6135df669b0a1f621dd0694141362aba6d8890c"
)
_BOUND_READ_FLAGS = (
    os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
)
_LINEAGE_FIELDS = (
    "st_dev",
    "st_ino",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
)
_ENV_KEYS = (
    "POSTGRES_USER",
    "POSTGRES_DB",
    "POSTGRES_PASSWORD_FILE",
    "MINIO_ROOT_USER_FILE",
    "MINIO_ROOT_PASSWORD_FILE",
    "CMMS_NGINX_RUNTIME_DIR",
)
_PINNED_IMAGES = MappingProxyType(
    {
        "postgres": (
            "postgres:16-alpine@sha256:"
            "7a396fd264a2067788b6551122b50f162bf6136312c7fc9d74381cb92c648382"
        ),
        "minio": (
            "minio/minio:RELEASE.2025-04-22T22-12-26Z@sha256:"
            "3f97c5651cb6662b880c787a232b6b34fec8d8922e08d6617b25d241a21164bb"
        ),
        "nginx": (
            "nginx:1.27.0-alpine@sha256:"
            "a377278b7dde3a8012b25d141d025a88dbf9f5ed13c5cdf21ee241e7ec07ab57"
        ),
    }
)
_F_ADD_SEALS = 1033
_F_GET_SEALS = 1034
_FULL_SEALS = 0x0001 | 0x0002 | 0x0004 | 0x0008
_CANONICAL_REPOSITORY = re.compile(r"[a-z0-9][a-z0-9._:/-]{0,254}\Z")


def _compose_error() -> DeploymentError:
    return DeploymentError("CMMS-E041", "CMMS Compose operation failed", 41)


def _bridge_error() -> DeploymentError:
    return DeploymentError("CMMS-E042", "Docker gateway is not verified", 42)


def _canonical_root(value: Path) -> Path:
    try:
        raw = os.fspath(value)
        root = Path(os.path.abspath(raw))
        metadata = root.lstat()
        if (
            raw != os.fspath(root)
            or not stat.S_ISDIR(metadata.st_mode)
            or root.resolve(strict=True) != root
        ):
            raise _compose_error()
    except DeploymentError:
        raise
    except (OSError, TypeError, ValueError):
        raise _compose_error() from None
    return root


def _require_compose_file(root: Path) -> Path:
    path = root / "deploy/compose/cmms-development.yml"
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or metadata.st_size <= 0
            or metadata.st_size > _MAX_COMPOSE_FILE_BYTES
            or path.resolve(strict=True) != path
        ):
            raise _compose_error()
    except DeploymentError:
        raise
    except (OSError, ValueError):
        raise _compose_error() from None
    return path


def _require_directory_metadata(metadata: os.stat_result, mode: int) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise _compose_error()


class _HeldRuntimeDirectories:
    def __init__(self, root: Path) -> None:
        self.runtime = root / ".runtime"
        self.nginx = self.runtime / "cmms-nginx"
        self._runtime_fd = -1
        self._nginx_fd = -1
        self._closed = False
        self._runtime_identity: tuple[int, int] | None = None
        self._nginx_identity: tuple[int, int] | None = None
        try:
            self._runtime_fd = os.open(
                self.runtime,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            runtime_metadata = os.fstat(self._runtime_fd)
            _require_directory_metadata(runtime_metadata, 0o700)
            self._runtime_identity = (
                runtime_metadata.st_dev,
                runtime_metadata.st_ino,
            )
            self._nginx_fd = os.open(
                "cmms-nginx",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=self._runtime_fd,
            )
            nginx_metadata = os.fstat(self._nginx_fd)
            _require_directory_metadata(nginx_metadata, 0o755)
            self._nginx_identity = (
                nginx_metadata.st_dev,
                nginx_metadata.st_ino,
            )
            self.verify()
        except Exception:
            self.close()
            raise

    def verify(self) -> None:
        if (
            self._closed
            or self._runtime_fd < 0
            or self._nginx_fd < 0
            or self._runtime_identity is None
            or self._nginx_identity is None
        ):
            raise _compose_error()
        fresh_runtime_fd = -1
        fresh_nginx_fd = -1
        try:
            held_runtime = os.fstat(self._runtime_fd)
            held_nginx = os.fstat(self._nginx_fd)
            _require_directory_metadata(held_runtime, 0o700)
            _require_directory_metadata(held_nginx, 0o755)
            if (
                (held_runtime.st_dev, held_runtime.st_ino)
                != self._runtime_identity
                or (held_nginx.st_dev, held_nginx.st_ino)
                != self._nginx_identity
            ):
                raise _compose_error()
            fresh_runtime_fd = os.open(
                self.runtime,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            )
            fresh_runtime = os.fstat(fresh_runtime_fd)
            _require_directory_metadata(fresh_runtime, 0o700)
            if (
                fresh_runtime.st_dev,
                fresh_runtime.st_ino,
            ) != self._runtime_identity:
                raise _compose_error()
            fresh_nginx_fd = os.open(
                "cmms-nginx",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=fresh_runtime_fd,
            )
            fresh_nginx = os.fstat(fresh_nginx_fd)
            _require_directory_metadata(fresh_nginx, 0o755)
            if (fresh_nginx.st_dev, fresh_nginx.st_ino) != self._nginx_identity:
                raise _compose_error()
        except DeploymentError:
            raise
        except (OSError, TypeError, ValueError):
            raise _compose_error() from None
        finally:
            if fresh_nginx_fd >= 0:
                try:
                    os.close(fresh_nginx_fd)
                except OSError:
                    pass
            if fresh_runtime_fd >= 0:
                try:
                    os.close(fresh_runtime_fd)
                except OSError:
                    pass

    def open_nginx_config(self) -> int:
        self.verify()
        try:
            return os.open(
                "nginx.conf",
                _BOUND_READ_FLAGS,
                dir_fd=self._nginx_fd,
            )
        except (OSError, TypeError, ValueError):
            raise _compose_error() from None

    def close(self) -> None:
        if not self._closed:
            if self._nginx_fd >= 0:
                try:
                    os.close(self._nginx_fd)
                except OSError:
                    pass
            if self._runtime_fd >= 0:
                try:
                    os.close(self._runtime_fd)
                except OSError:
                    pass
            self._closed = True


@contextmanager
def _held_runtime_directories(root: Path) -> Iterator[_HeldRuntimeDirectories]:
    held = _HeldRuntimeDirectories(root)
    try:
        held.verify()
        yield held
        held.verify()
    finally:
        held.close()


@dataclass(frozen=True)
class _BoundRuntimeFile:
    path: Path
    fd: int
    lineage: tuple[int, int, int, int, int]


def _lineage(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return tuple(  # type: ignore[return-value]
        getattr(metadata, field) for field in _LINEAGE_FIELDS
    )


@dataclass(frozen=True)
class _BoundComposeSource:
    path: Path
    fd: int
    lineage: tuple[int, int, int, int, int]
    data: bytes


@dataclass(frozen=True)
class _BoundNginxConfig:
    fd: int
    lineage: tuple[int, int, int, int, int]
    data: bytes


def _require_compose_metadata(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or metadata.st_size <= 0
        or metadata.st_size > _MAX_COMPOSE_FILE_BYTES
    ):
        raise _compose_error()


def _read_regular_fd(fd: int, size: int) -> bytes:
    if type(fd) is not int or fd < 0 or type(size) is not int or size <= 0:
        raise _compose_error()
    chunks: list[bytes] = []
    remaining = size + 1
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        raise _compose_error() from None
    data = b"".join(chunks)
    if len(data) != size:
        raise _compose_error()
    return data


def _open_bound_compose_source(path: Path) -> _BoundComposeSource:
    fd = -1
    try:
        fd = os.open(path, _BOUND_READ_FLAGS)
        before = os.fstat(fd)
        _require_compose_metadata(before)
        data = _read_regular_fd(fd, before.st_size)
        after = os.fstat(fd)
        _require_compose_metadata(after)
        if (
            _lineage(before) != _lineage(after)
            or hashlib.sha256(data).hexdigest() != _REVIEWED_COMPOSE_SHA256
        ):
            raise _compose_error()
        binding = _BoundComposeSource(path, fd, _lineage(after), data)
        _verify_bound_compose_source(binding)
        fd = -1
        return binding
    except DeploymentError:
        raise
    except (OSError, TypeError, ValueError):
        raise _compose_error() from None
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def _verify_bound_compose_source(binding: _BoundComposeSource) -> None:
    fresh_fd = -1
    try:
        held = os.fstat(binding.fd)
        _require_compose_metadata(held)
        if (
            _lineage(held) != binding.lineage
            or _read_regular_fd(binding.fd, held.st_size) != binding.data
            or hashlib.sha256(binding.data).hexdigest()
            != _REVIEWED_COMPOSE_SHA256
        ):
            raise _compose_error()
        fresh_fd = os.open(binding.path, _BOUND_READ_FLAGS)
        fresh = os.fstat(fresh_fd)
        _require_compose_metadata(fresh)
        if (
            _lineage(fresh) != binding.lineage
            or _read_regular_fd(fresh_fd, fresh.st_size) != binding.data
        ):
            raise _compose_error()
    except DeploymentError:
        raise
    except (OSError, TypeError, ValueError):
        raise _compose_error() from None
    finally:
        if fresh_fd >= 0:
            try:
                os.close(fresh_fd)
            except OSError:
                pass


@contextmanager
def _held_compose_source(path: Path) -> Iterator[_BoundComposeSource]:
    binding = _open_bound_compose_source(path)
    try:
        _verify_bound_compose_source(binding)
        yield binding
        _verify_bound_compose_source(binding)
    finally:
        try:
            os.close(binding.fd)
        except OSError:
            pass


def _require_nginx_config_metadata(
    metadata: os.stat_result,
    *,
    size: int,
) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o444
        or metadata.st_size != size
        or size <= 0
        or size > _MAX_NGINX_CONFIG_BYTES
    ):
        raise _compose_error()


def _verify_bound_nginx_config(
    runtime_directories: _HeldRuntimeDirectories,
    binding: _BoundNginxConfig,
) -> None:
    fresh_fd = -1
    try:
        runtime_directories.verify()
        held = os.fstat(binding.fd)
        _require_nginx_config_metadata(held, size=len(binding.data))
        if (
            _lineage(held) != binding.lineage
            or _read_regular_fd(binding.fd, held.st_size) != binding.data
        ):
            raise _compose_error()
        fresh_fd = runtime_directories.open_nginx_config()
        fresh = os.fstat(fresh_fd)
        _require_nginx_config_metadata(fresh, size=len(binding.data))
        if (
            _lineage(fresh) != binding.lineage
            or _read_regular_fd(fresh_fd, fresh.st_size) != binding.data
        ):
            raise _compose_error()
        runtime_directories.verify()
    except DeploymentError:
        raise
    except (OSError, TypeError, ValueError):
        raise _compose_error() from None
    finally:
        if fresh_fd >= 0:
            try:
                os.close(fresh_fd)
            except OSError:
                pass


@contextmanager
def _held_published_nginx_config(
    runtime_directories: _HeldRuntimeDirectories,
    expected: bytes,
) -> Iterator[_BoundNginxConfig]:
    fd = -1
    try:
        if (
            type(expected) is not bytes
            or not expected
            or len(expected) > _MAX_NGINX_CONFIG_BYTES
        ):
            raise _compose_error()
        runtime_directories.verify()
        fd = runtime_directories.open_nginx_config()
        before = os.fstat(fd)
        _require_nginx_config_metadata(before, size=len(expected))
        data = _read_regular_fd(fd, before.st_size)
        after = os.fstat(fd)
        _require_nginx_config_metadata(after, size=len(expected))
        if _lineage(before) != _lineage(after) or data != expected:
            raise _compose_error()
        binding = _BoundNginxConfig(fd, _lineage(after), data)
        _verify_bound_nginx_config(runtime_directories, binding)
        fd = -1
        try:
            yield binding
            _verify_bound_nginx_config(runtime_directories, binding)
        finally:
            try:
                os.close(binding.fd)
            except OSError:
                pass
    except DeploymentError:
        raise
    except (OSError, TypeError, ValueError):
        raise _compose_error() from None
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def _require_bound_metadata(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size <= 0
        or metadata.st_size > _MAX_RUNTIME_FILE_BYTES
    ):
        raise _compose_error()


def _open_bound_runtime_file(
    path: Path,
    *,
    policy: RuntimePathPolicy,
) -> _BoundRuntimeFile:
    parent_fd = -1
    fd = -1
    try:
        parent_fd, name = open_verified_parent(path, policy=policy)
        fd = os.open(name, _BOUND_READ_FLAGS, dir_fd=parent_fd)
        metadata = os.fstat(fd)
        _require_bound_metadata(metadata)
        binding = _BoundRuntimeFile(path, fd, _lineage(metadata))
        fd = -1
        return binding
    except DeploymentError:
        raise _compose_error() from None
    except (OSError, TypeError, ValueError):
        raise _compose_error() from None
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if parent_fd >= 0:
            try:
                os.close(parent_fd)
            except OSError:
                pass


def _verify_bound_runtime_file(
    binding: _BoundRuntimeFile,
    *,
    policy: RuntimePathPolicy,
) -> None:
    parent_fd = -1
    fresh_fd = -1
    try:
        held = os.fstat(binding.fd)
        _require_bound_metadata(held)
        if _lineage(held) != binding.lineage:
            raise _compose_error()
        parent_fd, name = open_verified_parent(binding.path, policy=policy)
        fresh_fd = os.open(name, _BOUND_READ_FLAGS, dir_fd=parent_fd)
        fresh = os.fstat(fresh_fd)
        _require_bound_metadata(fresh)
        if _lineage(fresh) != binding.lineage:
            raise _compose_error()
    except DeploymentError:
        raise _compose_error() from None
    except (OSError, TypeError, ValueError):
        raise _compose_error() from None
    finally:
        if fresh_fd >= 0:
            try:
                os.close(fresh_fd)
            except OSError:
                pass
        if parent_fd >= 0:
            try:
                os.close(parent_fd)
            except OSError:
                pass


class _HeldRuntimeFiles:
    def __init__(self, runtime: Path, paths: tuple[Path, ...]) -> None:
        if not paths or len(set(paths)) != len(paths):
            raise _compose_error()
        self._policy = RuntimePathPolicy.for_test(runtime, allowed_files=set(paths))
        self._bindings: list[_BoundRuntimeFile] = []
        self._closed = False
        try:
            for path in paths:
                self._bindings.append(
                    _open_bound_runtime_file(path, policy=self._policy)
                )
            self.verify()
        except Exception:
            self.close()
            raise

    def verify(self) -> None:
        if self._closed or not self._bindings:
            raise _compose_error()
        for binding in self._bindings:
            _verify_bound_runtime_file(binding, policy=self._policy)

    def close(self) -> None:
        if not self._closed:
            for binding in reversed(self._bindings):
                try:
                    os.close(binding.fd)
                except OSError:
                    pass
            self._closed = True


@contextmanager
def _held_secret_sources(
    root: Path,
    config: RuntimeConfig,
) -> Iterator[_HeldRuntimeFiles]:
    runtime = root / ".runtime"
    held = _HeldRuntimeFiles(
        runtime,
        (
            config.postgres_password_file,
            config.minio_root_user_file,
            config.minio_root_password_file,
        ),
    )
    try:
        held.verify()
        yield held
        held.verify()
    finally:
        held.close()


def _environment_bytes(
    config: RuntimeConfig,
    nginx_runtime: Path,
    secret_sources: _HeldRuntimeFiles,
) -> bytes:
    if type(config) is not RuntimeConfig:
        raise _compose_error()
    secret_sources.verify()
    values = {
        "POSTGRES_USER": config.postgres_user,
        "POSTGRES_DB": config.postgres_db,
        "POSTGRES_PASSWORD_FILE": os.fspath(config.postgres_password_file),
        "MINIO_ROOT_USER_FILE": os.fspath(config.minio_root_user_file),
        "MINIO_ROOT_PASSWORD_FILE": os.fspath(config.minio_root_password_file),
        "CMMS_NGINX_RUNTIME_DIR": os.fspath(nginx_runtime),
    }
    if tuple(values) != _ENV_KEYS or any(
        type(value) is not str
        or not value
        or "\x00" in value
        or "\n" in value
        or "\r" in value
        for value in values.values()
    ):
        raise _compose_error()
    try:
        return (
            "".join(f"{key}={values[key]}\n" for key in _ENV_KEYS).encode(
                "utf-8", errors="strict"
            )
        )
    except UnicodeError:
        raise _compose_error() from None


def _write_all(fd: int, data: bytes) -> None:
    offset = 0
    try:
        while offset < len(data):
            written = os.write(fd, data[offset:])
            if type(written) is not int or written <= 0:
                raise _compose_error()
            offset += written
    except DeploymentError:
        raise
    except OSError:
        raise _compose_error() from None


@contextmanager
def _sealed_snapshot(
    data: bytes,
    controller_pid: int,
    *,
    logical_name: str,
) -> Iterator[str]:
    fd = -1
    try:
        settings = {
            "ifactory-cmms-compose-env": (_MAX_OUTPUT_BYTES, 0o600),
            "ifactory-cmms-compose-file": (_MAX_COMPOSE_FILE_BYTES, 0o600),
            "ifactory-cmms-nginx-config": (_MAX_NGINX_CONFIG_BYTES, 0o444),
        }
        if (
            type(controller_pid) is not int
            or controller_pid != os.getpid()
            or logical_name not in settings
        ):
            raise _compose_error()
        maximum_size, mode = settings[logical_name]
        if type(data) is not bytes or not data or len(data) > maximum_size:
            raise _compose_error()
        flags = os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
        fd = os.memfd_create(logical_name, flags=flags)
        os.fchmod(fd, mode)
        _write_all(fd, data)
        os.lseek(fd, 0, os.SEEK_SET)
        fcntl.fcntl(fd, _F_ADD_SEALS, _FULL_SEALS)
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != mode
            or metadata.st_size != len(data)
            or fcntl.fcntl(fd, _F_GET_SEALS) & _FULL_SEALS != _FULL_SEALS
        ):
            raise _compose_error()
        yield f"/proc/{controller_pid}/fd/{fd}"
    except DeploymentError:
        raise
    except (AttributeError, OSError, TypeError, ValueError):
        raise _compose_error() from None
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


@contextmanager
def _sealed_environment(data: bytes, controller_pid: int) -> Iterator[str]:
    with _sealed_snapshot(
        data,
        controller_pid,
        logical_name="ifactory-cmms-compose-env",
    ) as path:
        yield path


@contextmanager
def _sealed_compose_file(data: bytes, controller_pid: int) -> Iterator[str]:
    with _sealed_snapshot(
        data,
        controller_pid,
        logical_name="ifactory-cmms-compose-file",
    ) as path:
        yield path


@contextmanager
def _sealed_nginx_config(data: bytes, controller_pid: int) -> Iterator[str]:
    with _sealed_snapshot(
        data,
        controller_pid,
        logical_name="ifactory-cmms-nginx-config",
    ) as path:
        yield path


@contextmanager
def _empty_docker_config(runtime: Path) -> Iterator[Path]:
    parent_fd = -1
    directory_fd = -1
    name = ""
    created = False
    cleanup_failed = False
    identity: tuple[int, int] | None = None
    try:
        parent_fd = os.open(
            runtime,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
        parent_metadata = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != os.getuid()
            or stat.S_IMODE(parent_metadata.st_mode) != 0o700
        ):
            raise _compose_error()
        for _attempt in range(32):
            candidate = f".cmms-docker-config-{secrets.token_hex(16)}"
            try:
                os.mkdir(candidate, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
            name = candidate
            created = True
            break
        if not created:
            raise _compose_error()
        directory_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=parent_fd,
        )
        metadata = os.fstat(directory_fd)
        identity = (metadata.st_dev, metadata.st_ino)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or os.listdir(directory_fd)
        ):
            raise _compose_error()
        yield Path(f"/proc/{os.getpid()}/fd/{directory_fd}")
    except DeploymentError:
        raise
    except (OSError, TypeError, ValueError):
        raise _compose_error() from None
    finally:
        if (
            directory_fd >= 0
            and parent_fd >= 0
            and name
            and identity is not None
        ):
            try:
                after = os.fstat(directory_fd)
                current = os.stat(
                    name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if (
                    (after.st_dev, after.st_ino) != identity
                    or (current.st_dev, current.st_ino) != identity
                    or not stat.S_ISDIR(after.st_mode)
                    or stat.S_IMODE(after.st_mode) != 0o700
                    or after.st_uid != os.getuid()
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
        if created and parent_fd >= 0:
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except OSError:
                cleanup_failed = True
        if parent_fd >= 0:
            try:
                os.close(parent_fd)
            except OSError:
                cleanup_failed = True
        if cleanup_failed:
            raise _compose_error()


@contextmanager
def _temporary_empty_docker_config() -> Iterator[Path]:
    directory = Path("/")
    directory_fd = -1
    created = False
    cleanup_failed = False
    identity: tuple[int, int] | None = None
    try:
        directory = Path(
            tempfile.mkdtemp(
                prefix="ifactory-cmms-docker-config-",
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
            raise _bridge_error()
        yield Path(f"/proc/{os.getpid()}/fd/{directory_fd}")
    except DeploymentError:
        raise
    except (OSError, TypeError, ValueError):
        raise _bridge_error() from None
    finally:
        if directory_fd >= 0 and created and identity is not None:
            try:
                after = os.fstat(directory_fd)
                current = directory.stat(follow_symlinks=False)
                if (
                    (after.st_dev, after.st_ino) != identity
                    or (current.st_dev, current.st_ino) != identity
                    or not stat.S_ISDIR(after.st_mode)
                    or stat.S_IMODE(after.st_mode) != 0o700
                    or after.st_uid != os.getuid()
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
            raise _bridge_error()


def _strict_result(
    value: object,
    *,
    require_empty_stdout: bool,
    require_empty_stderr: bool,
) -> CommandResult:
    if (
        type(value) is not CommandResult
        or type(value.returncode) is not int
        or value.returncode != 0
        or type(value.stdout) is not str
        or type(value.stderr) is not str
    ):
        raise _compose_error()
    try:
        if (
            len(value.stdout.encode("utf-8", errors="strict")) > _MAX_OUTPUT_BYTES
            or len(value.stderr.encode("utf-8", errors="strict")) > _MAX_OUTPUT_BYTES
            or (require_empty_stdout and value.stdout != "")
            or (require_empty_stderr and value.stderr != "")
        ):
            raise _compose_error()
    except UnicodeError:
        raise _compose_error() from None
    return value


class _ConfigGuard:
    def __init__(
        self,
        root: Path,
        expected: RuntimeConfig | None,
        context: ClaimedApplyContext | None = None,
        action: PlannedAction | None = None,
    ) -> None:
        if expected is not None and type(expected) is not RuntimeConfig:
            raise _compose_error()
        if (context is None) != (action is None):
            raise _compose_error()
        self._root = root
        self._context = context
        self._action = action
        self._closed = False
        self._environment_files: _HeldRuntimeFiles | None = None
        try:
            if self._context is not None:
                if self._action is None:
                    raise _compose_error()
                self._context.require_action(self._action)
            runtime = root / ".runtime"
            self._environment_files = _HeldRuntimeFiles(
                runtime,
                (
                    runtime / "cmms-development.env",
                    runtime / "cmms-frontend.env",
                ),
            )
            loaded = RuntimeConfig.load(root)
            self._environment_files.verify()
            if expected is not None and loaded != expected:
                raise _compose_error()
            self._expected = loaded if expected is None else expected
            self.verify()
        except Exception:
            self.close()
            raise

    @property
    def config(self) -> RuntimeConfig:
        if self._closed:
            raise _compose_error()
        return self._expected

    def verify(self) -> None:
        if self._closed or self._environment_files is None:
            raise _compose_error()
        if self._context is not None:
            if self._action is None:
                raise _compose_error()
            self._context.require_action(self._action)
        self._environment_files.verify()
        current = RuntimeConfig.load(self._root)
        self._environment_files.verify()
        if current != self._expected:
            raise _compose_error()
        if self._context is not None:
            if current.config_sha256 != self._context.plan.snapshot.config_sha256:
                raise _compose_error()
            self._context.require_action(self._action)

    def close(self) -> None:
        if not self._closed:
            if self._environment_files is not None:
                self._environment_files.close()
            self._closed = True


@dataclass(frozen=True)
class ImageReceipt:
    platform: str
    images: Mapping[str, str]

    def __post_init__(self) -> None:
        try:
            copied = dict(self.images)
        except (TypeError, ValueError):
            raise _compose_error() from None
        if self.platform != _PLATFORM or copied != dict(_PINNED_IMAGES):
            raise _compose_error()
        object.__setattr__(self, "images", MappingProxyType(copied))


class DockerCompose:
    """Exact, environment-isolated invocation of the static Compose project."""

    def __init__(
        self,
        root: Path,
        runner: Any,
        controller_pid: int | None = None,
    ) -> None:
        self._root = _canonical_root(Path(root))
        self._compose_file = _require_compose_file(self._root)
        with _held_compose_source(self._compose_file):
            pass
        self._runner = runner
        pid = os.getpid() if controller_pid is None else controller_pid
        if type(pid) is not int or pid <= 0 or pid != os.getpid():
            raise _compose_error()
        self._controller_pid = pid

    def _resolve_run(self, guard: _ConfigGuard) -> Callable[[CommandSpec], object]:
        guard.verify()
        try:
            run = getattr(self._runner, "run")
            if not callable(run):
                raise TypeError("runner is not callable")
        except Exception:
            raise _compose_error() from None
        guard.verify()
        return run

    def _run_compose(
        self,
        config: RuntimeConfig,
        suffix: tuple[str, ...],
        *,
        safe_label: str,
        guard: _ConfigGuard,
        run: Callable[[CommandSpec], object],
        require_empty_stdout: bool = False,
        require_empty_stderr: bool = False,
        timeout_seconds: float = 120.0,
    ) -> CommandResult:
        guard.verify()
        with _held_runtime_directories(self._root) as runtime_directories:
            with _held_compose_source(self._compose_file) as compose_source:
                with _held_secret_sources(self._root, config) as secret_sources:
                    guard.verify()
                    runtime_directories.verify()
                    _verify_bound_compose_source(compose_source)
                    secret_sources.verify()
                    environment = _environment_bytes(
                        config,
                        runtime_directories.nginx,
                        secret_sources,
                    )
                    with _sealed_environment(
                        environment,
                        self._controller_pid,
                    ) as env_path:
                        with _sealed_compose_file(
                            compose_source.data,
                            self._controller_pid,
                        ) as compose_path:
                            with _empty_docker_config(
                                runtime_directories.runtime
                            ) as docker_config:
                                guard.verify()
                                runtime_directories.verify()
                                _verify_bound_compose_source(compose_source)
                                secret_sources.verify()
                                spec = CommandSpec(
                                    argv=(
                                        _DOCKER,
                                        "--host",
                                        _DOCKER_HOST,
                                        "compose",
                                        "--project-name",
                                        _PROJECT,
                                        "--env-file",
                                        env_path,
                                        "--project-directory",
                                        os.fspath(self._root),
                                        "-f",
                                        compose_path,
                                        *suffix,
                                    ),
                                    cwd=self._root,
                                    environment={
                                        "PATH": _PATH,
                                        "DOCKER_CONFIG": os.fspath(docker_config),
                                    },
                                    timeout_seconds=timeout_seconds,
                                    stdout_limit=(
                                        0
                                        if require_empty_stdout
                                        else _MAX_OUTPUT_BYTES
                                    ),
                                    stderr_limit=(
                                        0
                                        if require_empty_stderr
                                        else _MAX_OUTPUT_BYTES
                                    ),
                                    safe_label=safe_label,
                                )
                                guard.verify()
                                runtime_directories.verify()
                                _verify_bound_compose_source(compose_source)
                                secret_sources.verify()
                                try:
                                    result = run(spec)
                                except Exception:
                                    raise _compose_error() from None
                                secret_sources.verify()
                                _verify_bound_compose_source(compose_source)
                                runtime_directories.verify()
                                guard.verify()
                                checked = _strict_result(
                                    result,
                                    require_empty_stdout=require_empty_stdout,
                                    require_empty_stderr=require_empty_stderr,
                                )
                                secret_sources.verify()
                                _verify_bound_compose_source(compose_source)
                                runtime_directories.verify()
                                guard.verify()
        guard.verify()
        return checked

    def _inspect_exact_images(
        self,
        *,
        guard: _ConfigGuard,
        run: Callable[[CommandSpec], object],
    ) -> dict[str, str]:
        guard.verify()
        with _held_runtime_directories(self._root) as runtime_directories:
            with _empty_docker_config(runtime_directories.runtime) as docker_config:
                guard.verify()
                runtime_directories.verify()
                spec = CommandSpec(
                    argv=(
                        _DOCKER,
                        "--host",
                        _DOCKER_HOST,
                        "image",
                        "inspect",
                        "--platform",
                        _PLATFORM,
                        "--format",
                        "json",
                        *_PINNED_IMAGES.values(),
                    ),
                    cwd=self._root,
                    environment={
                        "PATH": _PATH,
                        "DOCKER_CONFIG": os.fspath(docker_config),
                    },
                    timeout_seconds=60.0,
                    stdout_limit=_MAX_OUTPUT_BYTES,
                    stderr_limit=0,
                    safe_label="cmms-image-inspect",
                )
                guard.verify()
                runtime_directories.verify()
                try:
                    result = run(spec)
                except Exception:
                    raise _compose_error() from None
                runtime_directories.verify()
                guard.verify()
                checked = _strict_result(
                    result,
                    require_empty_stdout=False,
                    require_empty_stderr=True,
                )
                images = _parse_image_inspection(checked.stdout)
                runtime_directories.verify()
                guard.verify()
        guard.verify()
        return images

    def _expected_loopback_nginx_config(
        self,
        context: ClaimedApplyContext,
        guard: _ConfigGuard,
    ) -> bytes:
        guard.verify()
        try:
            rendered = Gateway(
                template=(
                    self._root
                    / "deploy/gateway/cmms-development-nginx.conf.template"
                ),
                runner=self._runner,
            ).render(
                GatewayMode.LOOPBACK,
                None,
                context.plan.snapshot.unit_generation,
            )
            data = rendered.text.encode("utf-8", errors="strict")
        except Exception:
            raise _compose_error() from None
        if not data or len(data) > _MAX_NGINX_CONFIG_BYTES:
            raise _compose_error()
        guard.verify()
        return data

    def _validate_published_loopback_nginx(
        self,
        context: ClaimedApplyContext,
        *,
        guard: _ConfigGuard,
        run: Callable[[CommandSpec], object],
        validated_operation: Callable[[], None],
    ) -> None:
        if not callable(validated_operation):
            raise _compose_error()
        expected = self._expected_loopback_nginx_config(context, guard)
        guard.verify()
        with _held_runtime_directories(self._root) as runtime_directories:
            with _held_published_nginx_config(
                runtime_directories,
                expected,
            ) as published:
                with _sealed_nginx_config(
                    published.data,
                    self._controller_pid,
                ) as candidate:
                    with _empty_docker_config(
                        runtime_directories.runtime
                    ) as docker_config:
                        guard.verify()
                        runtime_directories.verify()
                        _verify_bound_nginx_config(
                            runtime_directories,
                            published,
                        )
                        spec = CommandSpec(
                            argv=(
                                _DOCKER,
                                "--host",
                                _DOCKER_HOST,
                                "run",
                                "--pull",
                                "never",
                                "--rm",
                                "--network",
                                "none",
                                "--platform",
                                _PLATFORM,
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
                                (
                                    "type=bind,"
                                    f"source={candidate},"
                                    "target=/etc/ifactory-cmms/nginx.conf,"
                                    "readonly"
                                ),
                                _PINNED_IMAGES["nginx"],
                                "-t",
                                "-q",
                                "-c",
                                "/etc/ifactory-cmms/nginx.conf",
                            ),
                            cwd=self._root,
                            environment={
                                "PATH": _PATH,
                                "DOCKER_CONFIG": os.fspath(docker_config),
                            },
                            timeout_seconds=30.0,
                            stdout_limit=0,
                            stderr_limit=0,
                            safe_label="cmms-nginx-pre-up-validate",
                        )
                        guard.verify()
                        runtime_directories.verify()
                        _verify_bound_nginx_config(
                            runtime_directories,
                            published,
                        )
                        try:
                            result = run(spec)
                        except Exception:
                            raise _compose_error() from None
                        _verify_bound_nginx_config(
                            runtime_directories,
                            published,
                        )
                        runtime_directories.verify()
                        guard.verify()
                        _strict_result(
                            result,
                            require_empty_stdout=True,
                            require_empty_stderr=True,
                        )
                        _verify_bound_nginx_config(
                            runtime_directories,
                            published,
                        )
                        runtime_directories.verify()
                        guard.verify()
                        validated_operation()
                        _verify_bound_nginx_config(
                            runtime_directories,
                            published,
                        )
                        runtime_directories.verify()
                        guard.verify()
        guard.verify()

    def config_quiet(self, snapshot: RuntimeConfig) -> None:
        guard = _ConfigGuard(self._root, snapshot)
        try:
            run = self._resolve_run(guard)
            self._run_compose(
                snapshot,
                ("config", "-q"),
                safe_label="cmms-compose-config",
                guard=guard,
                run=run,
                require_empty_stdout=True,
                require_empty_stderr=True,
                timeout_seconds=60.0,
            )
        finally:
            guard.close()

    def _action_guard(
        self,
        context: ClaimedApplyContext,
        action: PlannedAction,
    ) -> _ConfigGuard:
        context.require_action(action)
        return _ConfigGuard(self._root, None, context, action)

    def pull_exact_images(self, context: ClaimedApplyContext) -> ImageReceipt:
        action = PlannedAction(ActionCode.IMAGES_PULL_EXACT)
        guard = self._action_guard(context, action)
        try:
            run = self._resolve_run(guard)
            self._run_compose(
                guard.config,
                ("pull", *_SERVICES),
                safe_label="cmms-compose-pull",
                guard=guard,
                run=run,
                timeout_seconds=300.0,
            )
            images = self._inspect_exact_images(guard=guard, run=run)
            guard.verify()
            return ImageReceipt(platform=_PLATFORM, images=images)
        finally:
            guard.close()

    def up_state_and_gateway(self, context: ClaimedApplyContext) -> None:
        create = PlannedAction(
            ActionCode.COMPOSE_CREATE_STATE_GATEWAY,
            ActionTargetKind.COMPOSE_RESOURCE_SET,
            _COMPOSE_TARGET,
        )
        start = PlannedAction(
            ActionCode.COMPOSE_START_STATE_GATEWAY,
            ActionTargetKind.COMPOSE_RESOURCE_SET,
            _COMPOSE_TARGET,
        )
        actions = context.plan.actions
        if create in actions and start not in actions:
            action = create
            suffix = (
                "up",
                "--detach",
                "--no-build",
                "--pull",
                "never",
                *_SERVICES,
            )
            label = "cmms-compose-create"
        elif start in actions and create not in actions:
            action = start
            suffix = ("start", *_SERVICES)
            label = "cmms-compose-start"
        else:
            raise _compose_error()
        guard = self._action_guard(context, action)
        try:
            run = self._resolve_run(guard)
            if action is create:
                self._inspect_exact_images(guard=guard, run=run)
                guard.verify()
                self._validate_published_loopback_nginx(
                    context,
                    guard=guard,
                    run=run,
                    validated_operation=lambda: self._run_compose(
                        guard.config,
                        suffix,
                        safe_label=label,
                        guard=guard,
                        run=run,
                        timeout_seconds=180.0,
                    ),
                )
                guard.verify()
            else:
                self._run_compose(
                    guard.config,
                    suffix,
                    safe_label=label,
                    guard=guard,
                    run=run,
                    timeout_seconds=180.0,
                )
        finally:
            guard.close()

    def stop_preserving_volumes(self, context: ClaimedApplyContext) -> None:
        action = PlannedAction(
            ActionCode.COMPOSE_STOP_STATE_GATEWAY,
            ActionTargetKind.COMPOSE_RESOURCE_SET,
            _COMPOSE_TARGET,
        )
        guard = self._action_guard(context, action)
        try:
            run = self._resolve_run(guard)
            self._run_compose(
                guard.config,
                ("stop", *_STOP_SERVICES),
                safe_label="cmms-compose-stop",
                guard=guard,
                run=run,
                timeout_seconds=120.0,
            )
        finally:
            guard.close()


def _json_without_duplicates(
    text: str,
    failure: Callable[[], DeploymentError],
) -> object:
    if type(text) is not str:
        raise failure()

    def pairs(pairs_value: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs_value:
            if type(key) is not str or key in result:
                raise failure()
            result[key] = value
        return result

    try:
        return json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(failure()),
        )
    except DeploymentError:
        raise
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
        raise failure() from None


def _valid_repo_digest_alias(value: object, expected_digest: str) -> bool:
    if (
        type(value) is not str
        or not value.isascii()
        or value != value.casefold()
        or value.count("@") != 1
    ):
        return False
    repository, digest = value.rsplit("@", 1)
    if (
        digest != expected_digest
        or _CANONICAL_REPOSITORY.fullmatch(repository) is None
        or repository.startswith("/")
        or repository.endswith("/")
        or "//" in repository
        or "/./" in repository
        or "/../" in repository
        or repository.rsplit("/", 1)[-1].find(":") >= 0
    ):
        return False
    return all(component not in {".", ".."} for component in repository.split("/"))


def _parse_image_inspection(text: str) -> dict[str, str]:
    if type(text) is not str or not text.endswith("\n") or "\r" in text:
        raise _compose_error()
    lines = text.splitlines(keepends=True)
    if len(lines) != len(_SERVICES) or any(
        not line.endswith("\n") or line == "\n" for line in lines
    ):
        raise _compose_error()
    result: dict[str, str] = {}
    required_keys = {"RepoDigests", "Os", "Architecture"}
    for service, reference, line in zip(
        _SERVICES,
        _PINNED_IMAGES.values(),
        lines,
        strict=True,
    ):
        row = _json_without_duplicates(line[:-1], _compose_error)
        if type(row) is not dict or not required_keys.issubset(row):
            raise _compose_error()
        _tagged_name, digest = reference.rsplit("@", 1)
        repo_digests = row["RepoDigests"]
        if (
            type(repo_digests) is not list
            or not repo_digests
            or len(repo_digests) != len(set(repo_digests))
            or not all(
                _valid_repo_digest_alias(value, digest)
                for value in repo_digests
            )
            or row["Os"] != "linux"
            or type(row["Os"]) is not str
            or row["Architecture"] != "amd64"
            or type(row["Architecture"]) is not str
        ):
            raise _compose_error()
        result[service] = reference
    if tuple(result) != _SERVICES or result != dict(_PINNED_IMAGES):
        raise _compose_error()
    return result


def _strict_probe_result(value: object) -> str:
    if (
        type(value) is not CommandResult
        or type(value.returncode) is not int
        or value.returncode != 0
        or type(value.stdout) is not str
        or not value.stdout
        or type(value.stderr) is not str
        or value.stderr != ""
    ):
        raise _bridge_error()
    try:
        if len(value.stdout.encode("utf-8", errors="strict")) > _MAX_OUTPUT_BYTES:
            raise _bridge_error()
    except UnicodeError:
        raise _bridge_error() from None
    return value.stdout


def _run_bridge_probe(
    run: Callable[[CommandSpec], object],
    argv: tuple[str, ...],
    safe_label: str,
    *,
    environment: Mapping[str, str],
) -> str:
    spec = CommandSpec(
        argv=argv,
        cwd=Path("/"),
        environment=environment,
        timeout_seconds=30.0,
        stdout_limit=_MAX_OUTPUT_BYTES,
        stderr_limit=0,
        safe_label=safe_label,
    )
    try:
        result = run(spec)
    except Exception:
        raise _bridge_error() from None
    return _strict_probe_result(result)


def _safe_gateway(address: IPv4Address) -> bool:
    return not (
        address.is_unspecified
        or address.is_loopback
        or address.is_multicast
        or address.is_link_local
        or address.is_reserved
    )


def _parse_bridge_network(text: str) -> tuple[IPv4Address, IPv4Network]:
    value = _json_without_duplicates(text, _bridge_error)
    if type(value) is not list or len(value) != 1 or type(value[0]) is not dict:
        raise _bridge_error()
    row = value[0]
    options = row.get("Options")
    ipam = row.get("IPAM")
    if (
        row.get("Name") != "bridge"
        or type(options) is not dict
        or options.get("com.docker.network.bridge.name") != "docker0"
        or type(ipam) is not dict
        or type(ipam.get("Config")) is not list
    ):
        raise _bridge_error()
    candidates: list[tuple[IPv4Address, IPv4Network]] = []
    for config in ipam["Config"]:
        if type(config) is not dict:
            raise _bridge_error()
        gateway_value = config.get("Gateway")
        subnet_value = config.get("Subnet")
        if type(gateway_value) is not str or type(subnet_value) is not str:
            continue
        try:
            gateway = ipaddress.ip_address(gateway_value)
            subnet = ipaddress.ip_network(subnet_value, strict=True)
        except ValueError:
            raise _bridge_error() from None
        if type(gateway) is IPv4Address and type(subnet) is IPv4Network:
            if str(gateway) != gateway_value or str(subnet) != subnet_value:
                raise _bridge_error()
            candidates.append((gateway, subnet))
    if len(candidates) != 1:
        raise _bridge_error()
    gateway, subnet = candidates[0]
    if not _safe_gateway(gateway) or gateway not in subnet:
        raise _bridge_error()
    return gateway, subnet


def _require_local_bridge(text: str, gateway: IPv4Address, subnet: IPv4Network) -> None:
    value = _json_without_duplicates(text, _bridge_error)
    if type(value) is not list or len(value) != 1 or type(value[0]) is not dict:
        raise _bridge_error()
    row = value[0]
    addresses = row.get("addr_info")
    if row.get("ifname") != "docker0" or type(addresses) is not list:
        raise _bridge_error()
    ipv4_rows = [
        item
        for item in addresses
        if type(item) is dict and item.get("family") == "inet"
    ]
    if len(ipv4_rows) != 1:
        raise _bridge_error()
    item = ipv4_rows[0]
    local = item.get("local")
    prefixlen = item.get("prefixlen")
    if type(local) is not str or type(prefixlen) is not int:
        raise _bridge_error()
    try:
        address = IPv4Address(local)
        configured = IPv4Network(f"{local}/{prefixlen}", strict=False)
    except (ipaddress.AddressValueError, ipaddress.NetmaskValueError, ValueError):
        raise _bridge_error() from None
    if (
        str(address) != local
        or address != gateway
        or configured != subnet
        or not _safe_gateway(address)
    ):
        raise _bridge_error()


def resolve_default_bridge_gateway(runner: Any) -> IPv4Address:
    try:
        run = getattr(runner, "run")
        if not callable(run):
            raise TypeError("runner is not callable")
    except Exception:
        raise _bridge_error() from None
    with _temporary_empty_docker_config() as docker_config:
        network = _run_bridge_probe(
            run,
            (
                _DOCKER,
                "--host",
                _DOCKER_HOST,
                "network",
                "inspect",
                "bridge",
            ),
            "cmms-docker-bridge",
            environment={
                "PATH": _PATH,
                "DOCKER_CONFIG": os.fspath(docker_config),
            },
        )
    gateway, subnet = _parse_bridge_network(network)
    local = _run_bridge_probe(
        run,
        (_IP, "-json", "address", "show", "dev", "docker0"),
        "cmms-local-bridge",
        environment={"PATH": _PATH},
    )
    _require_local_bridge(local, gateway, subnet)
    return gateway
