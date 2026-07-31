"""Descriptor-bound private file reads and atomic writes."""

from __future__ import annotations

import ctypes
import errno
import os
import secrets
import stat as stat_module
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType

from .errors import DeploymentError


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
_WRITE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | os.O_CLOEXEC
    | os.O_NOFOLLOW
    | os.O_NONBLOCK
)
_STABLE_STAT_FIELDS = (
    "st_dev",
    "st_ino",
    "st_size",
    "st_mtime_ns",
    "st_ctime_ns",
)
_RENAME_NOREPLACE = 1


def _unsafe_file() -> DeploymentError:
    return DeploymentError("CMMS-E001", "unsafe runtime artifact", 1)


def _invalid_config() -> DeploymentError:
    return DeploymentError("CMMS-E002", "invalid runtime artifact content", 2)


def _canonical_absolute(path: Path) -> Path:
    try:
        return Path(os.path.abspath(os.fspath(path)))
    except (OSError, TypeError, ValueError):
        raise _unsafe_file() from None


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class RuntimePathPolicy:
    """An exact lexical whitelist anchored below one absolute runtime root."""

    root: Path
    allowed_files: frozenset[Path]
    allowed_directories: frozenset[Path]

    def __post_init__(self) -> None:
        root = _canonical_absolute(self.root)
        files = frozenset(_canonical_absolute(path) for path in self.allowed_files)
        directories = frozenset(
            _canonical_absolute(path) for path in self.allowed_directories
        )
        if not root.is_absolute():
            raise _unsafe_file()
        if any(not _within(path, root) for path in files | directories):
            raise _unsafe_file()
        if root not in directories:
            raise _unsafe_file()
        if any(path == root for path in files):
            raise _unsafe_file()
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "allowed_files", files)
        object.__setattr__(self, "allowed_directories", directories)

    @classmethod
    def for_test(
        cls,
        root: Path,
        allowed_files: Collection[Path],
    ) -> RuntimePathPolicy:
        canonical_root = _canonical_absolute(root)
        canonical_files = frozenset(
            _canonical_absolute(path) for path in allowed_files
        )
        if any(not _within(path, canonical_root) for path in canonical_files):
            raise _unsafe_file()
        directories: set[Path] = {canonical_root}
        for path in canonical_files:
            parent = path.parent
            while _within(parent, canonical_root):
                directories.add(parent)
                if parent == canonical_root:
                    break
                parent = parent.parent
        return cls(
            root=canonical_root,
            allowed_files=canonical_files,
            allowed_directories=frozenset(directories),
        )

    def require_allowed_file(self, path: Path) -> Path:
        canonical = _canonical_absolute(path)
        if canonical not in self.allowed_files:
            raise _unsafe_file()
        return canonical

    def require_allowed_directory(self, path: Path) -> Path:
        canonical = _canonical_absolute(path)
        if canonical not in self.allowed_directories:
            raise _unsafe_file()
        return canonical


@dataclass(frozen=True)
class SecureSnapshot:
    """A verified descriptor and the exact bytes/stat observed through it."""

    path: Path
    fd: int = field(repr=False)
    stat: os.stat_result
    sha256: str
    data: bytes = field(repr=False)
    _closed: bool = field(default=False, init=False, repr=False, compare=False)

    def __enter__(self) -> SecureSnapshot:
        if self._closed:
            raise _unsafe_file()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if not self._closed:
            os.close(self.fd)
            object.__setattr__(self, "_closed", True)


def open_verified_parent(
    path: Path,
    *,
    policy: RuntimePathPolicy,
) -> tuple[int, str]:
    """Open every absolute parent component without ever following a symlink."""

    canonical = policy.require_allowed_file(path)
    policy.require_allowed_directory(canonical.parent)
    current_fd = -1
    try:
        current_fd = os.open("/", _DIRECTORY_FLAGS)
        for component in canonical.parent.parts[1:]:
            next_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        if not canonical.name or canonical.name in {".", ".."}:
            raise _unsafe_file()
        return current_fd, canonical.name
    except DeploymentError:
        if current_fd >= 0:
            os.close(current_fd)
        raise
    except OSError:
        if current_fd >= 0:
            os.close(current_fd)
        raise _unsafe_file() from None


def require_private_regular_file(
    metadata: os.stat_result,
    *,
    expected_uid: int,
) -> None:
    if (
        not stat_module.S_ISREG(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or metadata.st_nlink != 1
        or stat_module.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise _unsafe_file()


def require_stable_stat(
    before: os.stat_result,
    after: os.stat_result,
    *,
    actual_size: int,
) -> None:
    if any(getattr(before, name) != getattr(after, name) for name in _STABLE_STAT_FIELDS):
        raise _unsafe_file()
    if before.st_size != actual_size or after.st_size != actual_size:
        raise _unsafe_file()


def read_bounded(fd: int, *, max_bytes: int) -> bytes:
    if type(max_bytes) is not int or max_bytes < 0:
        raise _invalid_config()
    chunks: list[bytes] = []
    remaining = max_bytes + 1
    try:
        while remaining:
            chunk = os.read(fd, min(remaining, 65_536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError:
        raise _unsafe_file() from None
    data = b"".join(chunks)
    if len(data) > max_bytes:
        raise _invalid_config()
    return data


def _validate_required_content(data: bytes) -> None:
    if not data or b"\x00" in data:
        raise _invalid_config()


def read_secure_bytes(
    path: Path,
    *,
    max_bytes: int,
    policy: RuntimePathPolicy,
) -> bytes:
    canonical = policy.require_allowed_file(path)
    parent_fd, name = open_verified_parent(canonical, policy=policy)
    fd = -1
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=parent_fd)
        before = os.fstat(fd)
        require_private_regular_file(before, expected_uid=os.getuid())
        if before.st_size > max_bytes:
            raise _invalid_config()
        data = read_bounded(fd, max_bytes=max_bytes)
        after = os.fstat(fd)
        require_stable_stat(before, after, actual_size=len(data))
        _validate_required_content(data)
        return data
    except DeploymentError:
        raise
    except OSError:
        raise _unsafe_file() from None
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)


def _write_all(fd: int, data: bytes) -> None:
    offset = 0
    try:
        while offset < len(data):
            written = os.write(fd, data[offset:])
            if written <= 0:
                raise _unsafe_file()
            offset += written
    except DeploymentError:
        raise
    except OSError:
        raise _unsafe_file() from None


def _rename_exclusive(
    source: str,
    destination: str,
    *,
    directory_fd: int,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise _unsafe_file()
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        directory_fd,
        os.fsencode(source),
        directory_fd,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        if error_number in {errno.EEXIST, errno.ELOOP, errno.EISDIR, errno.ENOTDIR}:
            raise _unsafe_file()
        raise _unsafe_file()


def _validate_replaced_file(
    parent_fd: int,
    name: str,
    expected: bytes,
) -> None:
    fd = -1
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=parent_fd)
        before = os.fstat(fd)
        require_private_regular_file(before, expected_uid=os.getuid())
        data = read_bounded(fd, max_bytes=len(expected))
        after = os.fstat(fd)
        require_stable_stat(before, after, actual_size=len(data))
        if data != expected:
            raise _unsafe_file()
    except DeploymentError:
        raise
    except OSError:
        raise _unsafe_file() from None
    finally:
        if fd >= 0:
            os.close(fd)


def _validate_existing_target(parent_fd: int, name: str) -> None:
    fd = -1
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    except OSError:
        raise _unsafe_file() from None
    try:
        require_private_regular_file(os.fstat(fd), expected_uid=os.getuid())
    finally:
        os.close(fd)


def atomic_write_private(
    path: Path,
    data: bytes,
    *,
    replace: bool,
    policy: RuntimePathPolicy,
) -> None:
    if type(data) is not bytes or type(replace) is not bool:
        raise _invalid_config()
    canonical = policy.require_allowed_file(path)
    parent_fd, name = open_verified_parent(canonical, policy=policy)
    temporary_name = f".{name}.tmp-{secrets.token_hex(16)}"
    temporary_fd = -1
    temporary_exists = False
    try:
        if replace:
            _validate_existing_target(parent_fd, name)
        temporary_fd = os.open(
            temporary_name,
            _WRITE_FLAGS,
            0o600,
            dir_fd=parent_fd,
        )
        temporary_exists = True
        os.fchmod(temporary_fd, 0o600)
        require_private_regular_file(
            os.fstat(temporary_fd),
            expected_uid=os.getuid(),
        )
        _write_all(temporary_fd, data)
        os.fsync(temporary_fd)
        os.close(temporary_fd)
        temporary_fd = -1

        if replace:
            os.replace(
                temporary_name,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
        else:
            _rename_exclusive(
                temporary_name,
                name,
                directory_fd=parent_fd,
            )
        temporary_exists = False
        os.fsync(parent_fd)
        _validate_replaced_file(parent_fd, name, data)
    except DeploymentError:
        raise
    except OSError:
        raise _unsafe_file() from None
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        if temporary_exists:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except OSError:
                pass
        os.close(parent_fd)
