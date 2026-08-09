"""Reviewed CMMS source baselines and stable, non-ambient Git evidence."""

from __future__ import annotations

import hashlib
import os
import re
import signal
import stat
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import DeploymentError
from .process import (
    CommandResult,
    CommandSpec,
    ProcessDeadlineExceeded,
    ProcessOutputLimitExceeded,
    collect_bounded_process_output,
)
from .records import (
    ActionCode,
    BuildArtifact,
    ClaimedApplyContext,
    DependencyReceipt,
    DeploymentSnapshot,
    Operation,
    RuntimeProfile,
    SourceBinding,
    SourceStatus,
    ToolchainReceipt,
    strict_canonical_json_loads,
)
from .toolchains import (
    ToolchainManifest,
    VerifiedToolchainBinding,
    verify_toolchain_receipt,
)


_HEX40 = re.compile(r"[0-9a-f]{40}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_MANIFEST_MAX_BYTES = 256 * 1024
_REVIEWED_SENSITIVE_MANIFEST_SHA256 = (
    "ce9accd96699afa427ccafccc774c595"
    "e8ddc5d239d42f147c873b43bb4140e5"
)
_SENSITIVE_MANIFEST_MINT_TOKEN = object()

_MAX_BASELINE_FILES = 4096
_MAX_BASELINE_FILE_BYTES = 16 * 1024 * 1024
_MAX_BASELINE_TOTAL_BYTES = 64 * 1024 * 1024

_GIT_PATH = Path("/usr/bin/git")
_GIT_TIMEOUT_SECONDS = 20.0
_GIT_STDERR_LIMIT = 64 * 1024
_GIT_METADATA_LIMIT = 8 * 1024 * 1024
_GIT_DIFF_LIMIT = 16 * 1024 * 1024
_MAX_UNTRACKED_FILES = 4096
_MAX_UNTRACKED_FILE_BYTES = 16 * 1024 * 1024
_MAX_UNTRACKED_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_WORKTREE_SCAN_ENTRIES = 131_072
_GIT_ENVIRONMENT = {
    "GIT_ASKPASS": "/bin/false",
    "GIT_ATTR_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_NO_LAZY_FETCH": "1",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_SSH_COMMAND": "/bin/false",
    "GIT_TERMINAL_PROMPT": "0",
    "LANG": "C",
    "LC_ALL": "C",
    "PATH": "/usr/bin:/bin",
    "SSH_ASKPASS": "/bin/false",
}


def _manifest_error() -> DeploymentError:
    return DeploymentError("CMMS-E033", "invalid sensitive source manifest", 33)


def _source_error() -> DeploymentError:
    return DeploymentError("CMMS-E034", "CMMS source evidence is unavailable", 34)


def _require_hex(value: object, pattern: re.Pattern[str]) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise _manifest_error()
    return value


def _safe_relative_path(value: object) -> str:
    if type(value) is not str or not value or len(value) > 1024:
        raise _manifest_error()
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        raise _manifest_error() from None
    candidate = PurePosixPath(value)
    if (
        candidate.is_absolute()
        or value != candidate.as_posix()
        or "\\" in value
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise _manifest_error()
    return value


@dataclass(frozen=True)
class SensitiveFile:
    path: str
    sha256: str

    def __post_init__(self) -> None:
        _safe_relative_path(self.path)
        _require_hex(self.sha256, _HEX64)


@dataclass(frozen=True)
class SensitiveTree:
    path: str
    sha256: str

    def __post_init__(self) -> None:
        _safe_relative_path(self.path)
        _require_hex(self.sha256, _HEX64)


@dataclass(frozen=True, init=False)
class SensitiveManifest:
    """An exact, whole-file reviewed baseline; callers cannot mint variants."""

    schema_version: int
    cmms_gitlink: str
    files: tuple[SensitiveFile, ...]
    migration_tree: SensitiveTree
    sha256: str

    def __init__(
        self,
        token: object,
        *,
        schema_version: int,
        cmms_gitlink: str,
        files: tuple[SensitiveFile, ...],
        migration_tree: SensitiveTree,
        sha256: str,
    ) -> None:
        if token is not _SENSITIVE_MANIFEST_MINT_TOKEN:
            raise _manifest_error()
        if type(schema_version) is not int or schema_version != 1:
            raise _manifest_error()
        _require_hex(cmms_gitlink, _HEX40)
        if (
            type(files) is not tuple
            or not files
            or len(files) > _MAX_BASELINE_FILES
            or any(type(row) is not SensitiveFile for row in files)
        ):
            raise _manifest_error()
        paths = tuple(row.path for row in files)
        if paths != tuple(sorted(paths)) or len(set(paths)) != len(paths):
            raise _manifest_error()
        if type(migration_tree) is not SensitiveTree:
            raise _manifest_error()
        if any(
            path == migration_tree.path
            or path.startswith(f"{migration_tree.path}/")
            for path in paths
        ):
            raise _manifest_error()
        _require_hex(sha256, _HEX64)
        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "cmms_gitlink", cmms_gitlink)
        object.__setattr__(self, "files", files)
        object.__setattr__(self, "migration_tree", migration_tree)
        object.__setattr__(self, "sha256", sha256)

    @classmethod
    def load(cls, path: Path) -> SensitiveManifest:
        data = _read_manifest(path)
        digest = hashlib.sha256(data).hexdigest()
        if digest != _REVIEWED_SENSITIVE_MANIFEST_SHA256:
            raise _manifest_error()
        try:
            raw = strict_canonical_json_loads(
                data,
                max_bytes=_MANIFEST_MAX_BYTES,
                max_depth=8,
            )
            if type(raw) is not dict or set(raw) != {
                "schema_version",
                "cmms_gitlink",
                "files",
                "migration_tree",
            }:
                raise _manifest_error()
            raw_files = raw["files"]
            if type(raw_files) is not list:
                raise _manifest_error()
            files: list[SensitiveFile] = []
            for raw_file in raw_files:
                if type(raw_file) is not dict or set(raw_file) != {"path", "sha256"}:
                    raise _manifest_error()
                files.append(
                    SensitiveFile(
                        path=_safe_relative_path(raw_file["path"]),
                        sha256=_require_hex(raw_file["sha256"], _HEX64),
                    )
                )
            raw_tree = raw["migration_tree"]
            if type(raw_tree) is not dict or set(raw_tree) != {"path", "sha256"}:
                raise _manifest_error()
            return cls(
                _SENSITIVE_MANIFEST_MINT_TOKEN,
                schema_version=raw["schema_version"],
                cmms_gitlink=_require_hex(raw["cmms_gitlink"], _HEX40),
                files=tuple(files),
                migration_tree=SensitiveTree(
                    path=_safe_relative_path(raw_tree["path"]),
                    sha256=_require_hex(raw_tree["sha256"], _HEX64),
                ),
                sha256=digest,
            )
        except DeploymentError:
            raise
        except (KeyError, OSError, TypeError, ValueError):
            raise _manifest_error() from None


def _read_manifest(path: Path) -> bytes:
    try:
        manifest_path = Path(path)
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(manifest_path, flags)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size > _MANIFEST_MAX_BYTES:
                raise _manifest_error()
            data = bytearray()
            while len(data) <= _MANIFEST_MAX_BYTES:
                chunk = os.read(fd, min(65_536, _MANIFEST_MAX_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            after = os.fstat(fd)
            if (
                len(data) > _MANIFEST_MAX_BYTES
                or len(data) != before.st_size
                or _stat_identity(before) != _stat_identity(after)
            ):
                raise _manifest_error()
            return bytes(data)
        finally:
            os.close(fd)
    except DeploymentError:
        raise
    except (OSError, TypeError, ValueError):
        raise _manifest_error() from None


@dataclass(frozen=True)
class SensitiveBaselineResult:
    ok: bool
    code: str
    manifest_sha256: str
    sensitive_file_count: int
    migration_file_count: int

    def __post_init__(self) -> None:
        if type(self.ok) is not bool:
            raise _source_error()
        expected = "SENSITIVE_BASELINE_MATCHED" if self.ok else "SENSITIVE_BASELINE_CHANGED"
        if self.code != expected or _HEX64.fullmatch(self.manifest_sha256) is None:
            raise _source_error()
        if (
            type(self.sensitive_file_count) is not int
            or self.sensitive_file_count < 0
            or type(self.migration_file_count) is not int
            or self.migration_file_count < 0
        ):
            raise _source_error()


class _BaselineChanged(RuntimeError):
    pass


@dataclass
class _ReadBudget:
    maximum_files: int
    maximum_file_bytes: int
    maximum_total_bytes: int
    file_count: int = 0
    total_bytes: int = 0

    def claim(self, size: int) -> None:
        if (
            type(size) is not int
            or size < 0
            or size > self.maximum_file_bytes
            or self.file_count >= self.maximum_files
            or self.total_bytes + size > self.maximum_total_bytes
        ):
            raise _BaselineChanged
        self.file_count += 1
        self.total_bytes += size


@dataclass(frozen=True)
class _BaselineSample:
    file_hashes: tuple[tuple[str, str], ...]
    migration_sha256: str
    migration_file_count: int


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
        value.st_mode,
    )


def _relative_parts(relative: str) -> tuple[str, ...]:
    try:
        safe = _safe_relative_path(relative)
    except DeploymentError:
        raise _BaselineChanged from None
    return tuple(PurePosixPath(safe).parts)


def _open_root_directory(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = -1
    try:
        fd = os.open(path, flags)
        metadata = os.fstat(fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise OSError
        result = fd
        fd = -1
        return result
    except OSError:
        raise _BaselineChanged from None
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def _open_child_directory(parent_fd: int, name: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = -1
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
        metadata = os.fstat(fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise OSError
        result = fd
        fd = -1
        return result
    except OSError:
        raise _BaselineChanged from None
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def _open_parent(root_fd: int, parts: tuple[str, ...]) -> int:
    current = next_fd = -1
    try:
        current = os.dup(root_fd)
        for part in parts:
            next_fd = _open_child_directory(current, part)
            closing_fd = current
            current = -1
            os.close(closing_fd)
            current = next_fd
            next_fd = -1
        result = current
        current = -1
        return result
    except _BaselineChanged:
        raise
    except OSError:
        raise _BaselineChanged from None
    finally:
        for fd in (next_fd, current):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _hash_open_regular(fd: int, budget: _ReadBudget) -> tuple[str, int]:
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise _BaselineChanged
        budget.claim(before.st_size)
        digest = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(65_536, remaining))
            if not chunk:
                raise _BaselineChanged
            digest.update(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise _BaselineChanged
        after = os.fstat(fd)
        if _stat_identity(before) != _stat_identity(after):
            raise _BaselineChanged
        return digest.hexdigest(), before.st_size
    except OSError:
        raise _BaselineChanged from None


def _hash_regular_at(parent_fd: int, name: str, budget: _ReadBudget) -> tuple[str, int]:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError:
        raise _BaselineChanged from None
    try:
        return _hash_open_regular(fd, budget)
    finally:
        os.close(fd)


def _hash_relative_file(root_fd: int, relative: str, budget: _ReadBudget) -> str:
    parts = _relative_parts(relative)
    parent_fd = _open_parent(root_fd, parts[:-1])
    try:
        digest, _size = _hash_regular_at(parent_fd, parts[-1], budget)
        return digest
    finally:
        os.close(parent_fd)


def _walk_regular_files(
    directory_fd: int,
    relative_parts: tuple[str, ...],
    budget: _ReadBudget,
) -> list[tuple[str, str]]:
    try:
        names = sorted(os.listdir(directory_fd), key=os.fsencode)
    except OSError:
        raise _BaselineChanged from None
    rows: list[tuple[str, str]] = []
    for name in names:
        if type(name) is not str or name in {"", ".", ".."} or "/" in name:
            raise _BaselineChanged
        try:
            name.encode("utf-8", errors="strict")
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except (OSError, UnicodeEncodeError):
            raise _BaselineChanged from None
        next_parts = relative_parts + (name,)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = _open_child_directory(directory_fd, name)
            try:
                if (
                    os.fstat(child_fd).st_dev != metadata.st_dev
                    or os.fstat(child_fd).st_ino != metadata.st_ino
                ):
                    raise _BaselineChanged
                rows.extend(_walk_regular_files(child_fd, next_parts, budget))
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(metadata.st_mode):
            digest, _size = _hash_regular_at(directory_fd, name, budget)
            rows.append(("/".join(next_parts), digest))
        else:
            raise _BaselineChanged
    return rows


def _capture_baseline_once(cmms_root: Path, manifest: SensitiveManifest) -> _BaselineSample:
    root_fd = _open_root_directory(cmms_root)
    tree_parent = tree_fd = -1
    try:
        root_before = os.fstat(root_fd)
        budget = _ReadBudget(
            _MAX_BASELINE_FILES,
            _MAX_BASELINE_FILE_BYTES,
            _MAX_BASELINE_TOTAL_BYTES,
        )
        file_hashes = tuple(
            (row.path, _hash_relative_file(root_fd, row.path, budget))
            for row in manifest.files
        )
        tree_parts = _relative_parts(manifest.migration_tree.path)
        tree_parent = _open_parent(root_fd, tree_parts[:-1])
        tree_fd = _open_child_directory(tree_parent, tree_parts[-1])
        migration_rows = _walk_regular_files(tree_fd, tree_parts, budget)
        migration_rows.sort(key=lambda row: row[0])
        tree_digest = hashlib.sha256()
        for relative, digest in migration_rows:
            tree_digest.update(digest.encode("ascii"))
            tree_digest.update(b"  ")
            tree_digest.update(relative.encode("utf-8", errors="strict"))
            tree_digest.update(b"\n")
        root_after = os.fstat(root_fd)
        if _stat_identity(root_before) != _stat_identity(root_after):
            raise _BaselineChanged
        return _BaselineSample(
            file_hashes=file_hashes,
            migration_sha256=tree_digest.hexdigest(),
            migration_file_count=len(migration_rows),
        )
    except (OSError, UnicodeEncodeError):
        raise _BaselineChanged from None
    finally:
        cleanup_failed = False
        for fd in (tree_fd, tree_parent, root_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    cleanup_failed = True
        if cleanup_failed:
            raise _BaselineChanged from None


def verify_sensitive_baseline(
    cmms_root: Path,
    manifest: SensitiveManifest,
) -> SensitiveBaselineResult:
    if (
        type(manifest) is not SensitiveManifest
        or manifest.sha256 != _REVIEWED_SENSITIVE_MANIFEST_SHA256
    ):
        raise _source_error()
    try:
        first = _capture_baseline_once(Path(cmms_root), manifest)
        second = _capture_baseline_once(Path(cmms_root), manifest)
        expected_files = tuple((row.path, row.sha256) for row in manifest.files)
        ok = (
            first == second
            and first.file_hashes == expected_files
            and first.migration_sha256 == manifest.migration_tree.sha256
        )
        return SensitiveBaselineResult(
            ok=ok,
            code=("SENSITIVE_BASELINE_MATCHED" if ok else "SENSITIVE_BASELINE_CHANGED"),
            manifest_sha256=manifest.sha256,
            sensitive_file_count=(len(first.file_hashes) if ok else 0),
            migration_file_count=(first.migration_file_count if ok else 0),
        )
    except (_BaselineChanged, OSError, TypeError, ValueError):
        return SensitiveBaselineResult(
            ok=False,
            code="SENSITIVE_BASELINE_CHANGED",
            manifest_sha256=manifest.sha256,
            sensitive_file_count=0,
            migration_file_count=0,
        )


@dataclass(frozen=True)
class _UntrackedBinding:
    path: bytes = field(repr=False)
    kind: bytes
    size: int
    sha256: bytes = field(repr=False)


@dataclass(frozen=True)
class _RepositorySample:
    head: str
    dirty_fingerprint: str
    status: SourceStatus


@dataclass(frozen=True)
class _CompleteSourceSample:
    root: _RepositorySample
    cmms_gitlink: str
    cmms: _RepositorySample


def _terminate_git(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _git_bytes(
    repository: Path,
    arguments: tuple[str, ...],
    *,
    limit: int,
    input_bytes: bytes | None = None,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> bytes:
    if _GIT_PATH != Path("/usr/bin/git") or type(limit) is not int or limit < 0:
        raise _source_error()
    argv = (
        os.fspath(_GIT_PATH),
        "--no-optional-locks",
        "-c",
        "color.ui=false",
        "-c",
        "core.quotepath=false",
        "-c",
        "core.fsmonitor=false",
        *arguments,
    )
    try:
        process = subprocess.Popen(
            argv,
            cwd=repository,
            env=dict(_GIT_ENVIRONMENT),
            stdin=(subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )
    except (OSError, TypeError, ValueError):
        raise _source_error() from None
    deadline = time.monotonic() + _GIT_TIMEOUT_SECONDS
    try:
        stdout, _stderr = collect_bounded_process_output(
            process,
            input_bytes=input_bytes,
            deadline=deadline,
            stdout_limit=limit,
            stderr_limit=_GIT_STDERR_LIMIT,
        )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProcessDeadlineExceeded
        returncode = process.wait(timeout=remaining)
    except (
        OSError,
        ProcessDeadlineExceeded,
        ProcessOutputLimitExceeded,
        subprocess.TimeoutExpired,
        ValueError,
    ):
        _terminate_git(process)
        raise _source_error() from None
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
    if returncode not in allowed_returncodes:
        raise _source_error()
    return stdout


def _parse_object_id(raw: bytes) -> str:
    try:
        value = raw.rstrip(b"\n").decode("ascii", errors="strict")
    except UnicodeDecodeError:
        raise _source_error() from None
    if raw not in {value.encode("ascii"), value.encode("ascii") + b"\n"}:
        raise _source_error()
    if _HEX40.fullmatch(value) is None:
        raise _source_error()
    return value


def _frame(digest: Any, label: bytes, payload: bytes) -> None:
    digest.update(len(label).to_bytes(4, "big"))
    digest.update(label)
    digest.update(len(payload).to_bytes(8, "big"))
    digest.update(payload)


def _untracked_paths(status: bytes) -> tuple[bytes, ...]:
    paths: set[bytes] = set()
    records = status.split(b"\0")
    if records[-1] != b"":
        raise _source_error()
    for record in records[:-1]:
        if record.startswith(b"? "):
            path = record[2:]
            if not path or path in paths:
                raise _source_error()
            paths.add(path)
    if len(paths) > _MAX_UNTRACKED_FILES:
        raise _source_error()
    return tuple(sorted(paths))


def _untracked_parts(path: bytes) -> tuple[bytes, ...]:
    if (
        not path
        or path.startswith(b"/")
        or b"\0" in path
        or b"\\" in path
    ):
        raise _source_error()
    parts = tuple(path.split(b"/"))
    if any(part in {b"", b".", b".."} for part in parts):
        raise _source_error()
    return parts


def _open_bytes_parent(root_fd: int, parts: tuple[bytes, ...]) -> int:
    current = next_fd = -1
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        current = os.dup(root_fd)
        for part in parts:
            next_fd = os.open(part, flags, dir_fd=current)
            metadata = os.fstat(next_fd)
            if not stat.S_ISDIR(metadata.st_mode):
                raise OSError
            closing_fd = current
            current = -1
            os.close(closing_fd)
            current = next_fd
            next_fd = -1
        result = current
        current = -1
        return result
    except OSError:
        raise _source_error() from None
    finally:
        for fd in (next_fd, current):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _hash_untracked(
    repository_fd: int,
    path: bytes,
    budget: _ReadBudget,
) -> _UntrackedBinding:
    parts = _untracked_parts(path)
    parent = _open_bytes_parent(repository_fd, parts[:-1])
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        try:
            fd = os.open(parts[-1], flags, dir_fd=parent)
        except OSError:
            raise _source_error() from None
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                raise _source_error()
            try:
                budget.claim(before.st_size)
            except _BaselineChanged:
                raise _source_error() from None
            digest = hashlib.sha256()
            remaining = before.st_size
            while remaining:
                chunk = os.read(fd, min(65_536, remaining))
                if not chunk:
                    raise _source_error()
                digest.update(chunk)
                remaining -= len(chunk)
            if os.read(fd, 1):
                raise _source_error()
            after = os.fstat(fd)
            if _stat_identity(before) != _stat_identity(after):
                raise _source_error()
            return _UntrackedBinding(
                path=path,
                kind=b"regular",
                size=before.st_size,
                sha256=digest.digest(),
            )
        finally:
            os.close(fd)
    except OSError:
        raise _source_error() from None
    finally:
        os.close(parent)


def _repository_identity(path: Path) -> tuple[int, int, int, int, int, int]:
    try:
        metadata = os.stat(path, follow_symlinks=False)
    except OSError:
        raise _source_error() from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise _source_error()
    return _stat_identity(metadata)


def _parse_toplevel(raw: bytes, repository: Path) -> None:
    try:
        value = raw.rstrip(b"\n").decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise _source_error() from None
    if raw not in {value.encode("utf-8"), value.encode("utf-8") + b"\n"}:
        raise _source_error()
    if not value or Path(value) != repository:
        raise _source_error()


def _reject_unsafe_repository_config(repository: Path) -> None:
    raw = _git_bytes(
        repository,
        ("config", "--includes", "--name-only", "--null", "--list"),
        limit=_GIT_METADATA_LIMIT,
    )
    records = raw.split(b"\0")
    if records[-1] != b"":
        raise _source_error()
    for raw_key in records[:-1]:
        if not raw_key or b"\n" in raw_key or b"\r" in raw_key:
            raise _source_error()
        key = raw_key.lower()
        parts = key.split(b".")
        if (
            len(parts) >= 3
            and parts[0] == b"filter"
            and parts[-1] in {b"clean", b"process", b"smudge", b"required"}
        ):
            raise _source_error()
        if key == b"extensions.partialclone" or (
            len(parts) >= 3
            and parts[0] == b"remote"
            and parts[-1] in {b"promisor", b"partialclonefilter"}
        ):
            raise _source_error()


def _index_modes(repository: Path) -> dict[bytes, bytes]:
    raw = _git_bytes(
        repository,
        ("ls-files", "--stage", "-z"),
        limit=_GIT_METADATA_LIMIT,
    )
    records = raw.split(b"\0")
    if records[-1] != b"":
        raise _source_error()
    modes: dict[bytes, bytes] = {}
    for record in records[:-1]:
        metadata, separator, path = record.partition(b"\t")
        fields = metadata.split(b" ")
        if separator != b"\t" or len(fields) != 3 or not path or fields[2] != b"0":
            raise _source_error()
        mode, object_id, _stage = fields
        if (
            mode not in {b"100644", b"100755", b"120000", b"160000"}
            or _HEX40.fullmatch(object_id.decode("ascii", errors="ignore")) is None
            or path in modes
        ):
            raise _source_error()
        modes[path] = mode
    return modes


def _validate_index_visibility(
    repository: Path,
    index_modes: dict[bytes, bytes],
) -> None:
    raw = _git_bytes(
        repository,
        ("ls-files", "-v", "-z"),
        limit=_GIT_METADATA_LIMIT,
    )
    records = raw.split(b"\0")
    if records[-1] != b"":
        raise _source_error()
    paths: set[bytes] = set()
    for record in records[:-1]:
        if not record.startswith(b"H ") or len(record) <= 2:
            raise _source_error()
        path = record[2:]
        if path in paths:
            raise _source_error()
        paths.add(path)
    if paths != set(index_modes):
        raise _source_error()


_ROOT_APPROVED_GENERATED_DIRECTORIES = {
    b".runtime",
    b"deploy/cmms/.venv",
    b"deploy/cmms/.pytest_cache",
    b"deploy/cmms/.mypy_cache",
    b"deploy/cmms/.ruff_cache",
    b"deploy/cmms/src/ifactory_cmms_deploy/__pycache__",
    b"tests/.venv",
    b"tests/.pytest_cache",
    b"tests/.mypy_cache",
    b"tests/.ruff_cache",
    b"tests/__pycache__",
    b"tests/contract/__pycache__",
    b"tests/contract/phase1/__pycache__",
    b"tests/e2e/__pycache__",
    b"tests/e2e/support/__pycache__",
}
_CMMS_APPROVED_GENERATED_DIRECTORIES = {
    b"api/target",
    b"frontend/node_modules",
    b"frontend/build",
}


def _approved_generated_kind(domain: bytes, relative: bytes) -> bytes | None:
    if (
        domain == b"platform-root"
        and relative in _ROOT_APPROVED_GENERATED_DIRECTORIES
    ):
        return b"directory"
    if domain == b"cmms-component":
        if relative in _CMMS_APPROVED_GENERATED_DIRECTORIES:
            return b"directory"
        if relative == b"frontend/public/runtime-env.js":
            return b"regular"
    return None


def _scan_worktree_unindexed(
    repository_fd: int,
    index_modes: dict[bytes, bytes],
    *,
    domain: bytes,
) -> tuple[_UntrackedBinding, ...]:
    tracked_prefixes: set[bytes] = set()
    for tracked_path in index_modes:
        parts = tracked_path.split(b"/")
        tracked_prefixes.update(
            b"/".join(parts[:index]) for index in range(1, len(parts) + 1)
        )
    scanned = 0
    budget = _ReadBudget(
        _MAX_UNTRACKED_FILES,
        _MAX_UNTRACKED_FILE_BYTES,
        _MAX_UNTRACKED_TOTAL_BYTES,
    )
    bindings: list[_UntrackedBinding] = []

    def walk(directory_fd: int, prefix: bytes) -> None:
        nonlocal scanned
        try:
            names = sorted((os.fsencode(name) for name in os.listdir(directory_fd)))
        except OSError:
            raise _source_error() from None
        scanned += len(names)
        if scanned > _MAX_WORKTREE_SCAN_ENTRIES:
            raise _source_error()
        for name in names:
            if name in {b"", b".", b".."} or b"/" in name or b"\0" in name:
                raise _source_error()
            relative = name if not prefix else prefix + b"/" + name
            if relative == b".git":
                continue
            try:
                metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except OSError:
                raise _source_error() from None
            indexed_mode = index_modes.get(relative)
            generated_kind = _approved_generated_kind(domain, relative)
            has_tracked_descendant = relative in tracked_prefixes
            if stat.S_ISDIR(metadata.st_mode):
                if indexed_mode == b"160000":
                    continue
                if indexed_mode is not None:
                    raise _source_error()
                if generated_kind == b"directory" and not has_tracked_descendant:
                    continue
                if generated_kind is not None:
                    raise _source_error()
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                try:
                    child_fd = os.open(name, flags, dir_fd=directory_fd)
                except OSError:
                    raise _source_error() from None
                try:
                    child = os.fstat(child_fd)
                    if child.st_dev != metadata.st_dev or child.st_ino != metadata.st_ino:
                        raise _source_error()
                    walk(child_fd, relative)
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(metadata.st_mode):
                if indexed_mode is not None:
                    if indexed_mode not in {b"100644", b"100755"}:
                        raise _source_error()
                    executable = bool(stat.S_IMODE(metadata.st_mode) & 0o111)
                    if executable != (indexed_mode == b"100755"):
                        raise _source_error()
                    continue
                if generated_kind == b"regular" and not has_tracked_descendant:
                    continue
                if generated_kind is not None:
                    raise _source_error()
                bindings.append(_hash_untracked(repository_fd, relative, budget))
            elif stat.S_ISLNK(metadata.st_mode):
                if indexed_mode != b"120000":
                    raise _source_error()
            else:
                raise _source_error()

    walk(repository_fd, b"")
    bindings.sort(key=lambda row: row.path)
    return tuple(bindings)


def _capture_repository(
    repository: Path,
    *,
    domain: bytes,
    ignore_submodules: bool,
    additional_evidence: bytes = b"",
    force_dirty: bool = False,
) -> _RepositorySample:
    before_identity = _repository_identity(repository)
    try:
        repository_fd = os.open(
            repository,
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_CLOEXEC
            | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0),
        )
    except OSError:
        raise _source_error() from None
    try:
        if _stat_identity(os.fstat(repository_fd)) != before_identity:
            raise _source_error()
        _parse_toplevel(
            _git_bytes(
                repository,
                ("rev-parse", "--show-toplevel"),
                limit=4096,
            ),
            repository,
        )
        _reject_unsafe_repository_config(repository)
        head = _parse_object_id(
            _git_bytes(
                repository,
                ("rev-parse", "--verify", "HEAD^{commit}"),
                limit=256,
            )
        )
        index_modes = _index_modes(repository)
        _validate_index_visibility(repository, index_modes)
        submodule_option = (("--ignore-submodules=all",) if ignore_submodules else ())
        status = _git_bytes(
            repository,
            (
                "status",
                "--porcelain=v2",
                "-z",
                "--no-renames",
                "--untracked-files=all",
                *submodule_option,
            ),
            limit=_GIT_METADATA_LIMIT,
        )
        unstaged = _git_bytes(
            repository,
            (
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--binary",
                "--full-index",
                "--no-renames",
                "--diff-algorithm=minimal",
                "--no-indent-heuristic",
                *submodule_option,
                "--",
            ),
            limit=_GIT_DIFF_LIMIT,
        )
        staged = _git_bytes(
            repository,
            (
                "diff",
                "--cached",
                "--no-ext-diff",
                "--no-textconv",
                "--binary",
                "--full-index",
                "--no-renames",
                "--diff-algorithm=minimal",
                "--no-indent-heuristic",
                *submodule_option,
                "--",
            ),
            limit=_GIT_DIFF_LIMIT,
        )
        visible_untracked = _untracked_paths(status)
        untracked = _scan_worktree_unindexed(
            repository_fd,
            index_modes,
            domain=domain,
        )
        if not set(visible_untracked).issubset({row.path for row in untracked}):
            raise _source_error()
        fingerprint = hashlib.sha256()
        _frame(fingerprint, b"format", b"ifactory-cmms-dirty-fingerprint-v1")
        _frame(fingerprint, b"repository-domain", domain)
        _frame(fingerprint, b"additional-evidence", additional_evidence)
        _frame(fingerprint, b"porcelain-v2-z", status)
        _frame(fingerprint, b"unstaged-diff-sha256", hashlib.sha256(unstaged).digest())
        _frame(fingerprint, b"staged-diff-sha256", hashlib.sha256(staged).digest())
        _frame(fingerprint, b"untracked-count", len(untracked).to_bytes(8, "big"))
        for row in untracked:
            _frame(fingerprint, b"untracked-path", row.path)
            _frame(fingerprint, b"untracked-type", row.kind)
            _frame(fingerprint, b"untracked-length", row.size.to_bytes(8, "big"))
            _frame(fingerprint, b"untracked-sha256", row.sha256)
        after_identity = _repository_identity(repository)
        if (
            _stat_identity(os.fstat(repository_fd)) != before_identity
            or after_identity != before_identity
        ):
            raise _source_error()
        return _RepositorySample(
            head=head,
            dirty_fingerprint=fingerprint.hexdigest(),
            status=(
                SourceStatus.CLEAN
                if (
                    not force_dirty
                    and not status
                    and not unstaged
                    and not staged
                    and not untracked
                )
                else SourceStatus.UNCOMMITTED
            ),
        )
    finally:
        os.close(repository_fd)


def _root_gitlink(root: Path) -> str:
    raw = _git_bytes(
        root,
        ("ls-tree", "-z", "HEAD", "--", "components/cmms"),
        limit=1024,
    )
    prefix = b"160000 commit "
    suffix = b"\tcomponents/cmms\0"
    if not raw.startswith(prefix) or not raw.endswith(suffix):
        raise _source_error()
    object_id = raw[len(prefix) : -len(suffix)]
    return _parse_object_id(object_id)


def _root_index_gitlink(root: Path) -> str | None:
    raw = _git_bytes(
        root,
        ("ls-files", "--stage", "-z", "--", "components/cmms"),
        limit=1024,
    )
    if raw == b"":
        return None
    prefix = b"160000 "
    suffix = b" 0\tcomponents/cmms\0"
    if not raw.startswith(prefix) or not raw.endswith(suffix):
        raise _source_error()
    object_id = raw[len(prefix) : -len(suffix)]
    return _parse_object_id(object_id)


class SourceInspector:
    """Capture two stable, independent root and CMMS Git evidence samples."""

    def _capture_complete_once(self, source_root: Path) -> _CompleteSourceSample:
        gitlink = _root_gitlink(source_root)
        index_gitlink = _root_index_gitlink(source_root)
        index_payload = (index_gitlink or "INDEX_GITLINK_ABSENT").encode("ascii")
        root_sample = _capture_repository(
            source_root,
            domain=b"platform-root",
            ignore_submodules=True,
            additional_evidence=(
                gitlink.encode("ascii") + b"\0" + index_payload
            ),
            force_dirty=index_gitlink != gitlink,
        )
        cmms_sample = _capture_repository(
            source_root / "components/cmms",
            domain=b"cmms-component",
            ignore_submodules=False,
        )
        return _CompleteSourceSample(root_sample, gitlink, cmms_sample)

    def capture(self, root: Path, profile: RuntimeProfile) -> SourceBinding:
        if type(profile) is not RuntimeProfile:
            raise _source_error()
        try:
            source_root = Path(os.path.realpath(os.path.abspath(os.fspath(root))))
        except (OSError, TypeError, ValueError):
            raise _source_error() from None
        first = self._capture_complete_once(source_root)
        second = self._capture_complete_once(source_root)
        if first != second:
            raise _source_error()
        if profile is RuntimeProfile.ACCEPTANCE and (
            first.root.status is not SourceStatus.CLEAN
            or first.cmms.status is not SourceStatus.CLEAN
            or first.cmms_gitlink != first.cmms.head
        ):
            raise _source_error()
        return SourceBinding(
            root_sha=first.root.head,
            root_dirty_fingerprint=first.root.dirty_fingerprint,
            root_status=first.root.status,
            cmms_gitlink=first.cmms_gitlink,
            cmms_head=first.cmms.head,
            cmms_dirty_fingerprint=first.cmms.dirty_fingerprint,
            cmms_status=first.cmms.status,
        )


def build_api_artifact(
    context: ClaimedApplyContext,
    toolchains: ToolchainReceipt,
    runner: object,
) -> BuildArtifact:
    """Build the reviewed CMMS API artifact for one live claimed apply."""

    if type(context) is not ClaimedApplyContext or type(toolchains) is not ToolchainReceipt:
        raise _build_error()
    context.require_action(ActionCode.BUILD_API)

    root, runtime_root, manifest = _resolve_toolchain_root_and_manifest(
        context,
        toolchains,
        _build_error,
    )
    cmms_root = root / "components/cmms"
    api_root = cmms_root / "api"
    settings_path = root / "deploy/cmms/maven-settings.xml"
    sensitive_path = root / "deploy/cmms/manifests/startup-sensitive-files.json"
    sensitive = SensitiveManifest.load(sensitive_path)
    snapshot = context.plan.snapshot
    if (
        sensitive.sha256 != snapshot.sensitive_manifest_sha256
        or sensitive.cmms_gitlink != snapshot.source.cmms_gitlink
        or sensitive.cmms_gitlink != snapshot.source.cmms_head
    ):
        raise _build_error()

    protected: list[_GeneratedPathBinding] = []
    root_protected: list[_ExactIgnoredTreeBinding] = []
    api_target_phase: list[_GeneratedPathBinding] = []
    private_directories: list[_PrivateDirectoryBinding] = []
    runtime_boundary: _RuntimeBoundaryBinding | None = None
    try:
        root_protected.extend(_capture_root_ignored_trees(root, _build_error))
        runtime_boundary = _capture_runtime_boundary(
            runtime_root,
            (
                _RuntimeOwnedRule(("cmms-cache", "maven"), "directory"),
                _RuntimeOwnedRule(("cmms-builds",), "directory"),
            ),
            mutable_content=set(),
            creation_allowed=set(),
            controlled_paths={
                ("cmms-cache",),
                ("cmms-cache", "maven"),
                ("cmms-builds",),
            },
            contained_roots={("cmms-cache", "maven")},
            failure=_build_error,
        )
        api_target_phase.append(
            _capture_generated_path(
                api_root / "target",
                "directory",
                _build_error,
            )
        )
        frontend_root = cmms_root / "frontend"
        for path, expected_kind in (
            (frontend_root / "node_modules", "directory"),
            (frontend_root / "build", "directory"),
            (frontend_root / "public/runtime-env.js", "regular"),
        ):
            protected.append(
                _capture_generated_path(path, expected_kind, _build_error)
            )
        initial_pom_sha256 = _verify_api_inputs(
            root,
            cmms_root,
            api_root,
            settings_path,
            sensitive,
            context,
        )

        def require_bound_paths() -> None:
            if runtime_boundary is None:
                raise _build_error()
            for binding in root_protected:
                binding.verify()
            runtime_boundary.verify()
            for binding in api_target_phase:
                binding.verify()
            for binding in protected:
                binding.verify()
            for binding in private_directories:
                binding.verify()

        def require_api_state() -> None:
            try:
                require_bound_paths()
                if (
                    _verify_api_inputs(
                        root,
                        cmms_root,
                        api_root,
                        settings_path,
                        sensitive,
                        context,
                    )
                    != initial_pom_sha256
                ):
                    raise _build_error()
            except DeploymentError:
                raise _build_error() from None

        require_api_state()
        runner = _resolve_runner_for_action(
            context,
            runner,
            ActionCode.BUILD_API,
            require_api_state,
            _build_error,
        )
        return _build_api_artifact_bound(
            context,
            toolchains,
            runner,
            root,
            manifest,
            cmms_root,
            api_root,
            settings_path,
            sensitive,
            snapshot,
            initial_pom_sha256,
            require_api_state,
            require_bound_paths,
            private_directories,
            runtime_boundary,
            api_target_phase,
        )
    finally:
        for binding in reversed(private_directories):
            binding.close()
        for binding in reversed(api_target_phase):
            binding.close()
        for binding in reversed(protected):
            binding.close()
        if runtime_boundary is not None:
            runtime_boundary.close()
        for binding in reversed(root_protected):
            binding.close()


def _build_api_artifact_bound(
    context: ClaimedApplyContext,
    toolchains: ToolchainReceipt,
    runner: object,
    root: Path,
    manifest: ToolchainManifest,
    cmms_root: Path,
    api_root: Path,
    settings_path: Path,
    sensitive: SensitiveManifest,
    snapshot: DeploymentSnapshot,
    initial_pom_sha256: str,
    require_api_state: Callable[[], None],
    require_bound_paths: Callable[[], None],
    private_directories: list[_PrivateDirectoryBinding],
    runtime_boundary: _RuntimeBoundaryBinding,
    api_target_phase: list[_GeneratedPathBinding],
) -> BuildArtifact:
    verified_root, verified_manifest, toolchain_binding = _verify_build_toolchains(
        context,
        toolchains,
        runner,
        probe_state_check=require_api_state,
    )
    if verified_root != root or verified_manifest != manifest:
        raise _build_error()
    require_api_state()
    cache_root = root / ".runtime/cmms-cache"
    maven_cache = cache_root / "maven"
    for parts, directory in (
        (("cmms-cache",), cache_root),
        (("cmms-cache", "maven"), maven_cache),
    ):
        context.require_action(ActionCode.BUILD_API)
        runtime_boundary.allow_controlled_creation(parts)
        context.require_action(ActionCode.BUILD_API)
        _ensure_private_directory(directory)
        context.require_action(ActionCode.BUILD_API)
        private_directories.append(
            _capture_private_directory(directory, _build_error)
        )
        runtime_boundary.seal_owned_paths()
        require_api_state()
    context.require_action(ActionCode.BUILD_API)
    require_api_state()
    context.require_action(ActionCode.BUILD_API)
    if len(api_target_phase) != 1:
        raise _build_error()
    api_target_phase.pop().close()
    runtime_boundary.release_controlled_path(("cmms-cache", "maven"))
    context.require_action(ActionCode.BUILD_API)
    require_api_state()
    maven_home = toolchains.require("maven").home
    temurin_home = toolchains.require("temurin").home
    environment = {
        "JAVA_HOME": os.fspath(temurin_home),
        "LC_ALL": "C",
        "PATH": (
            f"{maven_home / 'bin'}:{temurin_home / 'bin'}:/usr/bin:/bin"
        ),
    }
    command_prefix = (
        os.fspath(maven_home / "bin/mvn"),
        "--settings",
        os.fspath(settings_path),
        f"-Dmaven.repo.local={maven_cache}",
    )
    targeted_tests_passed = not (
        context.plan.profile is RuntimeProfile.DEVELOPMENT
        and context.plan.operation is Operation.RESTART_API
    )
    commands: list[CommandSpec] = []
    if targeted_tests_passed:
        commands.append(
            _maven_command(
                (
                    *command_prefix,
                    "-Dtest=AssetControllerTest,WorkOrderControllerTest,AssetIntegrationServiceTest,IntegrationIdempotencyServiceTest,WorkOrderServiceTest",
                    "-Dsurefire.failIfNoSpecifiedTests=true",
                    "test",
                ),
                api_root,
                environment,
                "cmms-api-targeted-tests",
            )
        )
    commands.append(
        _maven_command(
            (*command_prefix, "clean", "package", "-DskipTests"),
            api_root,
            environment,
            "cmms-api-package",
        )
    )
    for command in commands:
        context.require_action(ActionCode.BUILD_API)
        require_api_state()
        context.require_action(ActionCode.BUILD_API)
        _verify_build_toolchains(
            context,
            toolchains,
            runner,
            toolchain_binding,
            reprobe=False,
        )
        context.require_action(ActionCode.BUILD_API)
        require_api_state()
        context.require_action(ActionCode.BUILD_API)
        try:
            result = runner.run(command)
        except Exception:
            raise _build_error() from None
        context.require_action(ActionCode.BUILD_API)
        require_api_state()
        context.require_action(ActionCode.BUILD_API)
        _verify_build_toolchains(
            context,
            toolchains,
            runner,
            toolchain_binding,
            reprobe=False,
        )
        context.require_action(ActionCode.BUILD_API)
        if (
            type(result) is not CommandResult
            or type(result.returncode) is not int
            or result.returncode != 0
            or type(result.stdout) is not str
            or type(result.stderr) is not str
        ):
            raise _build_error()
        require_api_state()

    context.require_action(ActionCode.BUILD_API)
    runtime_boundary.freeze_controlled_path(("cmms-cache", "maven"))
    api_target_phase.append(
        _capture_generated_path(
            api_root / "target",
            "directory",
            _build_error,
        )
    )
    context.require_action(ActionCode.BUILD_API)
    require_api_state()
    context.require_action(ActionCode.BUILD_API)
    _verify_build_toolchains(
        context,
        toolchains,
        runner,
        toolchain_binding,
        reprobe=False,
    )
    context.require_action(ActionCode.BUILD_API)
    require_api_state()
    context.require_action(ActionCode.BUILD_API)
    _verify_build_toolchains(
        context,
        toolchains,
        runner,
        toolchain_binding,
        probe_state_check=require_api_state,
    )
    context.require_action(ActionCode.BUILD_API)
    require_api_state()
    context.require_action(ActionCode.BUILD_API)
    runtime_boundary.release_controlled_path(("cmms-builds",))
    context.require_action(ActionCode.BUILD_API)
    prepared = _prepare_api_candidate(
        api_root / "target",
        root / ".runtime/cmms-builds",
    )
    try:
        context.require_action(ActionCode.BUILD_API)
        prepared.verify()
        context.require_action(ActionCode.BUILD_API)
        require_api_state()
        context.require_action(ActionCode.BUILD_API)
        artifact_binding = prepared.finalize()
        context.require_action(ActionCode.BUILD_API)
        require_api_state()
        context.require_action(ActionCode.BUILD_API)
        prepared.verify_published(artifact_binding)
        return BuildArtifact(
            source=snapshot.source,
            toolchain_manifest_sha256=manifest.sha256,
            sensitive_manifest_sha256=sensitive.sha256,
            artifact_sha256=artifact_binding.sha256,
            path=artifact_binding.path,
            targeted_tests_passed=targeted_tests_passed,
        )
    finally:
        prepared.close()


def install_frontend_dependencies(
    context: ClaimedApplyContext,
    toolchains: ToolchainReceipt,
    runner: object,
) -> DependencyReceipt:
    """Verify the reviewed CMMS frontend dependency graph."""

    if type(context) is not ClaimedApplyContext or type(toolchains) is not ToolchainReceipt:
        raise _frontend_error()
    context.require_action(ActionCode.FRONTEND_VERIFY)
    root, runtime_root, manifest = _resolve_toolchain_root_and_manifest(
        context,
        toolchains,
        _frontend_error,
    )
    sensitive_path = root / "deploy/cmms/manifests/startup-sensitive-files.json"
    sensitive = SensitiveManifest.load(sensitive_path)
    snapshot = context.plan.snapshot
    if (
        sensitive.sha256 != snapshot.sensitive_manifest_sha256
        or sensitive.cmms_gitlink != snapshot.source.cmms_gitlink
        or sensitive.cmms_gitlink != snapshot.source.cmms_head
    ):
        raise _frontend_error()
    expected_lock_sha256 = _frontend_lock_sha256(sensitive)
    cmms_root = root / "components/cmms"
    frontend_root = cmms_root / "frontend"
    lock = _open_frontend_lock(
        frontend_root / "package-lock.json",
        expected_lock_sha256,
    )
    userconfig_path = (
        runtime_root
        / "cmms-cache"
        / f"npm-userconfig-{context.plan.plan_sha256}.npmrc"
    )
    userconfig: _EmptyUserConfigBinding | None = None
    api_target: _GeneratedPathBinding | None = None
    output_phase: list[_GeneratedPathBinding] = []
    root_protected: list[_ExactIgnoredTreeBinding] = []
    runtime_boundary: _RuntimeBoundaryBinding | None = None
    try:
        root_protected.extend(_capture_root_ignored_trees(root, _frontend_error))
        runtime_boundary = _capture_runtime_boundary(
            runtime_root,
            (
                _RuntimeOwnedRule(("cmms-cache", "npm"), "directory"),
                _RuntimeOwnedRule(
                    ("cmms-cache", userconfig_path.name),
                    "regular",
                ),
            ),
            mutable_content=set(),
            creation_allowed=set(),
            controlled_paths={
                ("cmms-cache",),
                ("cmms-cache", "npm"),
                ("cmms-cache", userconfig_path.name),
            },
            contained_roots={("cmms-cache", "npm")},
            failure=_frontend_error,
        )
        api_target = _capture_generated_path(
            cmms_root / "api/target",
            "directory",
            _frontend_error,
        )
        for path, expected_kind in (
            (frontend_root / "node_modules", "directory"),
            (frontend_root / "build", "directory"),
            (frontend_root / "public/runtime-env.js", "regular"),
        ):
            output_phase.append(
                _capture_generated_path(path, expected_kind, _frontend_error)
            )
        hooks = _capture_frontend_hooks(cmms_root)

        def require_bound_paths() -> None:
            if api_target is None or runtime_boundary is None:
                raise _frontend_error()
            for binding in root_protected:
                binding.verify()
            runtime_boundary.verify()
            api_target.verify()
            for binding in output_phase:
                binding.verify()

        def require_source_state() -> None:
            try:
                require_bound_paths()
                _verify_frontend_state(
                    root,
                    cmms_root,
                    sensitive_path,
                    sensitive,
                    context,
                    lock,
                    hooks,
                    api_target,
                )
            except DeploymentError:
                raise _frontend_error() from None

        require_source_state()
        runner = _resolve_runner_for_action(
            context,
            runner,
            ActionCode.FRONTEND_VERIFY,
            require_source_state,
            _frontend_error,
        )
        context.require_action(ActionCode.FRONTEND_VERIFY)
        _verified_root, _verified_manifest, toolchain_binding = (
            _verify_toolchains_for_action(
                context,
                toolchains,
                runner,
                ActionCode.FRONTEND_VERIFY,
                probe_state_check=require_source_state,
            )
        )
        if _verified_root != root or _verified_manifest != manifest:
            raise _frontend_error()
        context.require_action(ActionCode.FRONTEND_VERIFY)
        require_source_state()
        context.require_action(ActionCode.FRONTEND_VERIFY)

        cache_root = root / ".runtime/cmms-cache"
        npm_cache = cache_root / "npm"
        for parts, directory in (
            (("cmms-cache",), cache_root),
            (("cmms-cache", "npm"), npm_cache),
        ):
            context.require_action(ActionCode.FRONTEND_VERIFY)
            runtime_boundary.allow_controlled_creation(parts)
            context.require_action(ActionCode.FRONTEND_VERIFY)
            _ensure_frontend_private_directory(directory)
            context.require_action(ActionCode.FRONTEND_VERIFY)
            runtime_boundary.seal_owned_paths()
            require_source_state()
        context.require_action(ActionCode.FRONTEND_VERIFY)
        runtime_boundary.allow_controlled_creation(
            ("cmms-cache", userconfig_path.name)
        )
        context.require_action(ActionCode.FRONTEND_VERIFY)
        userconfig = _open_frontend_userconfig(userconfig_path)
        context.require_action(ActionCode.FRONTEND_VERIFY)
        runtime_boundary.seal_owned_paths()

        def require_runtime_state() -> None:
            if userconfig is None:
                raise _frontend_error()
            _verify_frontend_private_directories(
                runtime_root,
                cache_root,
                npm_cache,
            )
            userconfig.verify()
            require_source_state()

        require_runtime_state()
        context.require_action(ActionCode.FRONTEND_VERIFY)
        if len(output_phase) != 3:
            raise _frontend_error()
        for binding in reversed(output_phase):
            binding.close()
        output_phase.clear()
        runtime_boundary.release_controlled_path(("cmms-cache", "npm"))
        context.require_action(ActionCode.FRONTEND_VERIFY)
        require_runtime_state()

        node_home = toolchains.require("node").home
        environment = {
            "HUSKY": "0",
            "LC_ALL": "C",
            "NPM_CONFIG_USERCONFIG": os.fspath(userconfig_path),
            "PATH": f"{node_home / 'bin'}:/usr/bin:/bin",
        }
        commands = (
            CommandSpec(
                argv=(
                    os.fspath(node_home / "bin/npm"),
                    "ci",
                    "--legacy-peer-deps",
                    "--cache",
                    os.fspath(npm_cache),
                ),
                cwd=frontend_root,
                environment=environment,
                timeout_seconds=1_800,
                stdout_limit=4 * 1024 * 1024,
                stderr_limit=4 * 1024 * 1024,
                safe_label="cmms-frontend-npm-ci",
            ),
            CommandSpec(
                argv=(os.fspath(node_home / "bin/npm"), "run", "build"),
                cwd=frontend_root,
                environment=environment,
                timeout_seconds=1_800,
                stdout_limit=4 * 1024 * 1024,
                stderr_limit=4 * 1024 * 1024,
                safe_label="cmms-frontend-build",
            ),
        )
        for command in commands:
            context.require_action(ActionCode.FRONTEND_VERIFY)
            _verify_toolchains_for_action(
                context,
                toolchains,
                runner,
                ActionCode.FRONTEND_VERIFY,
                toolchain_binding,
                probe_state_check=require_runtime_state,
                reprobe=False,
            )
            context.require_action(ActionCode.FRONTEND_VERIFY)
            require_runtime_state()
            context.require_action(ActionCode.FRONTEND_VERIFY)
            try:
                result = runner.run(command)  # type: ignore[attr-defined]
            except Exception:
                raise _frontend_error() from None
            context.require_action(ActionCode.FRONTEND_VERIFY)
            if (
                type(result) is not CommandResult
                or type(result.returncode) is not int
                or result.returncode != 0
                or type(result.stdout) is not str
                or type(result.stderr) is not str
            ):
                raise _frontend_error()
            require_runtime_state()
            context.require_action(ActionCode.FRONTEND_VERIFY)
            _verify_toolchains_for_action(
                context,
                toolchains,
                runner,
                ActionCode.FRONTEND_VERIFY,
                toolchain_binding,
                probe_state_check=require_runtime_state,
                reprobe=False,
            )
            context.require_action(ActionCode.FRONTEND_VERIFY)
            require_runtime_state()
            context.require_action(ActionCode.FRONTEND_VERIFY)

        context.require_action(ActionCode.FRONTEND_VERIFY)
        runtime_boundary.freeze_controlled_path(("cmms-cache", "npm"))
        for path, expected_kind in (
            (frontend_root / "node_modules", "directory"),
            (frontend_root / "build", "directory"),
            (frontend_root / "public/runtime-env.js", "regular"),
        ):
            output_phase.append(
                _capture_generated_path(path, expected_kind, _frontend_error)
            )
        context.require_action(ActionCode.FRONTEND_VERIFY)
        require_runtime_state()
        context.require_action(ActionCode.FRONTEND_VERIFY)
        _verify_toolchains_for_action(
            context,
            toolchains,
            runner,
            ActionCode.FRONTEND_VERIFY,
            toolchain_binding,
            probe_state_check=require_runtime_state,
        )
        context.require_action(ActionCode.FRONTEND_VERIFY)
        require_runtime_state()
        context.require_action(ActionCode.FRONTEND_VERIFY)
        require_bound_paths()
        userconfig.verify()
        return DependencyReceipt(
            source=snapshot.source,
            toolchain_manifest_sha256=manifest.sha256,
            sensitive_manifest_sha256=sensitive.sha256,
            frontend_lock_sha256=lock.sha256,
        )
    finally:
        for binding in reversed(output_phase):
            binding.close()
        if userconfig is not None:
            userconfig.close()
        if api_target is not None:
            api_target.close()
        if runtime_boundary is not None:
            runtime_boundary.close()
        for binding in reversed(root_protected):
            binding.close()
        lock.close()


def _frontend_error() -> DeploymentError:
    return DeploymentError(
        "CMMS-E036",
        "CMMS frontend dependencies are not verified",
        36,
    )


def _frontend_lock_sha256(sensitive: SensitiveManifest) -> str:
    rows = tuple(
        row.sha256
        for row in sensitive.files
        if row.path == "frontend/package-lock.json"
    )
    if len(rows) != 1:
        raise _frontend_error()
    return rows[0]


_MAVEN_SETTINGS_SHA256 = (
    "a5c9e4d330b90b0850fb5cba07151dee"
    "43db594b8a5fdb972acf6817f16ddec8"
)
_MAX_DESCRIPTOR_BYTES = 2 * 1024 * 1024
_MAX_FRONTEND_LOCK_BYTES = 4 * 1024 * 1024
_MAX_API_ARTIFACT_BYTES = 512 * 1024 * 1024
_MAX_API_TARGET_ENTRIES = 131_072
_MAX_API_TARGET_DEPTH = 64
_MAX_GENERATED_TREE_ENTRIES = 262_144
_MAX_GENERATED_TREE_DEPTH = 64
_MAX_GENERATED_FILE_BYTES = 512 * 1024 * 1024
_MAX_GENERATED_TOTAL_BYTES = 8 * 1024 * 1024 * 1024
_MAX_GENERATED_SYMLINK_BYTES = 16 * 1024


def _build_error() -> DeploymentError:
    return DeploymentError("CMMS-E035", "CMMS API build is not verified", 35)


def _stable_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _read_stable_descriptor(path: Path, maximum: int) -> bytes:
    fd = -1
    try:
        if path.resolve(strict=True) != path:
            raise _build_error()
        before_path = path.lstat()
        if (
            not stat.S_ISREG(before_path.st_mode)
            or before_path.st_nlink != 1
            or before_path.st_size <= 0
            or before_path.st_size > maximum
        ):
            raise _build_error()
        fd = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
        before = os.fstat(fd)
        if _stable_identity(before) != _stable_identity(before_path):
            raise _build_error()
        data = bytearray()
        remaining = before.st_size
        while remaining:
            chunk = os.read(fd, min(65_536, remaining))
            if not chunk:
                raise _build_error()
            data.extend(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise _build_error()
        after = os.fstat(fd)
        after_path = path.lstat()
        if (
            _stable_identity(before) != _stable_identity(after)
            or _stable_identity(before) != _stable_identity(after_path)
            or path.resolve(strict=True) != path
        ):
            raise _build_error()
        return bytes(data)
    except DeploymentError:
        raise
    except (OSError, TypeError, ValueError):
        raise _build_error() from None
    finally:
        if fd >= 0:
            os.close(fd)


def _verify_build_toolchains(
    context: ClaimedApplyContext,
    receipt: ToolchainReceipt,
    runner: object,
    expected: VerifiedToolchainBinding | None = None,
    probe_state_check: Callable[[], None] | None = None,
    *,
    reprobe: bool = True,
) -> tuple[Path, ToolchainManifest, VerifiedToolchainBinding]:
    return _verify_toolchains_for_action(
        context,
        receipt,
        runner,
        ActionCode.BUILD_API,
        expected,
        probe_state_check,
        reprobe=reprobe,
    )


def _verify_toolchains_for_action(
    context: ClaimedApplyContext,
    receipt: ToolchainReceipt,
    runner: object,
    action: ActionCode,
    expected: VerifiedToolchainBinding | None = None,
    probe_state_check: Callable[[], None] | None = None,
    *,
    reprobe: bool = True,
) -> tuple[Path, ToolchainManifest, VerifiedToolchainBinding]:
    if action not in {ActionCode.BUILD_API, ActionCode.FRONTEND_VERIFY}:
        raise _build_error()
    failure = (
        _frontend_error
        if action is ActionCode.FRONTEND_VERIFY
        else _build_error
    )
    root, runtime_root, manifest = _resolve_toolchain_root_and_manifest(
        context,
        receipt,
        failure,
    )
    binding = verify_toolchain_receipt(
        manifest,
        receipt,
        _ActionGuardedProbeRunner(
            context,
            runner,
            action,
            probe_state_check,
        ),
        runtime_root=runtime_root,
        expected=expected,
        reprobe=reprobe,
    )
    return root, manifest, binding


def _resolve_toolchain_root_and_manifest(
    context: ClaimedApplyContext,
    receipt: ToolchainReceipt,
    failure: Callable[[], DeploymentError] = _build_error,
) -> tuple[Path, Path, ToolchainManifest]:
    installations = receipt.installations
    try:
        toolchains_root = installations[0].home.parent
        runtime_root = toolchains_root.parent
        root = runtime_root.parent
        if (
            runtime_root.name != ".runtime"
            or toolchains_root.name != "toolchains"
            or root.resolve(strict=True) != root
            or runtime_root.resolve(strict=True) != runtime_root
            or toolchains_root.resolve(strict=True) != toolchains_root
        ):
            raise failure()
    except (OSError, IndexError, TypeError, ValueError):
        raise failure() from None
    manifest = ToolchainManifest.load(root / "deploy/cmms/manifests/toolchains.json")
    if (
        receipt.manifest_sha256 != manifest.sha256
        or context.plan.snapshot.toolchain_manifest_sha256 != manifest.sha256
    ):
        raise failure()
    return root, runtime_root, manifest


class _ResolvedRunner:
    def __init__(self, run: Callable[[CommandSpec], object]) -> None:
        self._run = run

    def run(self, spec: CommandSpec) -> object:
        return self._run(spec)


def _resolve_runner_for_action(
    context: ClaimedApplyContext,
    runner: object,
    action: ActionCode,
    state_check: Callable[[], None],
    failure: Callable[[], DeploymentError],
) -> _ResolvedRunner:
    context.require_action(action)
    state_check()
    context.require_action(action)
    try:
        run = getattr(runner, "run")
        if not callable(run):
            raise TypeError("runner is not callable")
    except Exception:
        raise failure() from None
    context.require_action(action)
    state_check()
    context.require_action(action)
    return _ResolvedRunner(run)


class _ActionGuardedProbeRunner:
    def __init__(
        self,
        context: ClaimedApplyContext,
        runner: object,
        action: ActionCode,
        state_check: Callable[[], None] | None = None,
    ) -> None:
        self._context = context
        self._runner = runner
        self._action = action
        self._state_check = state_check

    def run(self, spec: CommandSpec) -> object:
        self._context.require_action(self._action)
        if self._state_check is not None:
            self._state_check()
            self._context.require_action(self._action)
        try:
            result = self._runner.run(spec)  # type: ignore[attr-defined]
        except Exception:
            failure = (
                _frontend_error
                if self._action is ActionCode.FRONTEND_VERIFY
                else _build_error
            )
            raise failure() from None
        self._context.require_action(self._action)
        if self._state_check is not None:
            self._state_check()
            self._context.require_action(self._action)
        return result


def _verify_api_inputs(
    root: Path,
    cmms_root: Path,
    api_root: Path,
    settings_path: Path,
    sensitive: SensitiveManifest,
    context: ClaimedApplyContext,
) -> str:
    if SourceInspector().capture(root, context.plan.profile) != context.plan.snapshot.source:
        raise _build_error()
    baseline = verify_sensitive_baseline(cmms_root, sensitive)
    if (
        type(baseline) is not SensitiveBaselineResult
        or not baseline.ok
        or baseline.manifest_sha256 != sensitive.sha256
    ):
        raise _build_error()
    settings = _read_stable_descriptor(settings_path, _MAX_DESCRIPTOR_BYTES)
    if hashlib.sha256(settings).hexdigest() != _MAVEN_SETTINGS_SHA256:
        raise _build_error()
    pom = _read_stable_descriptor(api_root / "pom.xml", _MAX_DESCRIPTOR_BYTES)
    if pom.count(b"<finalName") != 1 or pom.count(b"<finalName>app</finalName>") != 1:
        raise _build_error()
    return hashlib.sha256(pom).hexdigest()


def _ensure_private_directory(path: Path) -> Path:
    parent_fd = -1
    directory_fd = -1
    completed = False
    try:
        parent = path.parent
        if parent.resolve(strict=True) != parent or not parent.is_dir():
            raise _build_error()
        parent_fd = os.open(
            parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        created = False
        try:
            os.mkdir(path.name, 0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
        if created:
            os.chmod(
                path.name,
                0o700,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        directory_fd = os.open(
            path.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        metadata = os.fstat(directory_fd)
        path_metadata = path.lstat()
        if (
            path.resolve(strict=True) != path
            or not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or metadata.st_uid != os.getuid()
            or (metadata.st_dev, metadata.st_ino)
            != (path_metadata.st_dev, path_metadata.st_ino)
        ):
            raise _build_error()
        completed = True
        return path
    except DeploymentError:
        raise
    except (OSError, TypeError, ValueError):
        raise _build_error() from None
    finally:
        cleanup_failed = False
        if directory_fd >= 0:
            try:
                os.close(directory_fd)
            except OSError:
                cleanup_failed = True
        if parent_fd >= 0:
            try:
                os.close(parent_fd)
            except OSError:
                cleanup_failed = True
        if completed and cleanup_failed:
            raise _build_error() from None


def _private_directory_identity(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_uid)


@dataclass
class _PrivateDirectoryBinding:
    path: Path
    parent_fd: int = field(repr=False)
    directory_fd: int = field(repr=False)
    parent_identity: tuple[int, ...] = field(repr=False)
    directory_identity: tuple[int, ...] = field(repr=False)
    failure: Callable[[], DeploymentError] = field(repr=False)
    closed: bool = field(default=False, init=False, repr=False)

    def verify(self) -> None:
        if self.closed or self.parent_fd < 0 or self.directory_fd < 0:
            raise self.failure()
        try:
            parent_descriptor = os.fstat(self.parent_fd)
            parent_path = self.path.parent.lstat()
            descriptor = os.fstat(self.directory_fd)
            path_metadata = os.stat(
                self.path.name,
                dir_fd=self.parent_fd,
                follow_symlinks=False,
            )
            if (
                self.path.parent.resolve(strict=True) != self.path.parent
                or self.path.resolve(strict=True) != self.path
                or _private_directory_identity(parent_descriptor)
                != self.parent_identity
                or _private_directory_identity(parent_path) != self.parent_identity
                or _private_directory_identity(descriptor)
                != self.directory_identity
                or _private_directory_identity(path_metadata)
                != self.directory_identity
                or not stat.S_ISDIR(parent_descriptor.st_mode)
                or not stat.S_ISDIR(descriptor.st_mode)
                or parent_descriptor.st_uid != os.getuid()
                or descriptor.st_uid != os.getuid()
                or stat.S_IMODE(parent_descriptor.st_mode) != 0o700
                or stat.S_IMODE(descriptor.st_mode) != 0o700
            ):
                raise self.failure()
        except DeploymentError:
            raise
        except (OSError, TypeError, ValueError):
            raise self.failure() from None

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for field_name in ("directory_fd", "parent_fd"):
            fd = getattr(self, field_name)
            setattr(self, field_name, -1)
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _capture_private_directory(
    path: Path,
    failure: Callable[[], DeploymentError],
) -> _PrivateDirectoryBinding:
    parent_fd = directory_fd = -1
    try:
        parent = path.parent
        if parent.resolve(strict=True) != parent or path.resolve(strict=True) != path:
            raise failure()
        parent_fd = os.open(
            parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        directory_fd = os.open(
            path.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        binding = _PrivateDirectoryBinding(
            path,
            parent_fd,
            directory_fd,
            _private_directory_identity(os.fstat(parent_fd)),
            _private_directory_identity(os.fstat(directory_fd)),
            failure,
        )
        binding.verify()
        parent_fd = directory_fd = -1
        return binding
    except DeploymentError:
        raise
    except (OSError, TypeError, ValueError):
        raise failure() from None
    finally:
        for fd in (directory_fd, parent_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _ensure_frontend_private_directory(path: Path) -> Path:
    try:
        return _ensure_private_directory(path)
    except DeploymentError:
        raise _frontend_error() from None


def _digest_frontend_fd(fd: int, size: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        chunk = os.pread(fd, min(65_536, size - offset), offset)
        if not chunk:
            raise _frontend_error()
        digest.update(chunk)
        offset += len(chunk)
    if os.pread(fd, 1, size):
        raise _frontend_error()
    return digest.hexdigest()


@dataclass
class _OpenFrontendLock:
    path: Path
    fd: int = field(repr=False)
    identity: tuple[int, ...] = field(repr=False)
    sha256: str
    size: int
    closed: bool = field(default=False, init=False, repr=False)

    def verify(self) -> None:
        if self.closed or self.fd < 0:
            raise _frontend_error()
        try:
            descriptor = os.fstat(self.fd)
            path_metadata = self.path.lstat()
            if (
                self.path.resolve(strict=True) != self.path
                or _stable_identity(descriptor) != self.identity
                or _stable_identity(path_metadata) != self.identity
                or not stat.S_ISREG(descriptor.st_mode)
                or descriptor.st_nlink != 1
                or descriptor.st_size != self.size
                or self.size <= 0
                or self.size > _MAX_FRONTEND_LOCK_BYTES
                or _digest_frontend_fd(self.fd, self.size) != self.sha256
                or _stable_identity(os.fstat(self.fd)) != self.identity
            ):
                raise _frontend_error()
        except DeploymentError:
            raise
        except (OSError, TypeError, ValueError):
            raise _frontend_error() from None

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        fd = self.fd
        self.fd = -1
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def _open_frontend_lock(path: Path, expected_sha256: str) -> _OpenFrontendLock:
    fd = -1
    try:
        if path.resolve(strict=True) != path:
            raise _frontend_error()
        path_before = path.lstat()
        fd = os.open(
            path,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
        )
        descriptor = os.fstat(fd)
        identity = _stable_identity(descriptor)
        if (
            identity != _stable_identity(path_before)
            or not stat.S_ISREG(descriptor.st_mode)
            or descriptor.st_nlink != 1
            or descriptor.st_size <= 0
            or descriptor.st_size > _MAX_FRONTEND_LOCK_BYTES
        ):
            raise _frontend_error()
        digest = _digest_frontend_fd(fd, descriptor.st_size)
        binding = _OpenFrontendLock(
            path,
            fd,
            identity,
            digest,
            descriptor.st_size,
        )
        binding.verify()
        if digest != expected_sha256:
            raise _frontend_error()
        fd = -1
        return binding
    except DeploymentError:
        raise
    except (OSError, TypeError, ValueError):
        raise _frontend_error() from None
    finally:
        if fd >= 0:
            os.close(fd)


@dataclass
class _EmptyUserConfigBinding:
    path: Path
    parent_fd: int = field(repr=False)
    fd: int = field(repr=False)
    parent_identity: tuple[int, ...] = field(repr=False)
    identity: tuple[int, ...] = field(repr=False)
    closed: bool = field(default=False, init=False, repr=False)

    def verify(self) -> None:
        if self.closed or self.parent_fd < 0 or self.fd < 0:
            raise _frontend_error()
        try:
            parent_descriptor = os.fstat(self.parent_fd)
            parent_path = self.path.parent.lstat()
            descriptor = os.fstat(self.fd)
            path_metadata = os.stat(
                self.path.name,
                dir_fd=self.parent_fd,
                follow_symlinks=False,
            )
            if (
                self.path.parent.resolve(strict=True) != self.path.parent
                or self.path.resolve(strict=True) != self.path
                or _stable_identity(parent_descriptor) != self.parent_identity
                or _stable_identity(parent_path) != self.parent_identity
                or not stat.S_ISDIR(parent_descriptor.st_mode)
                or parent_descriptor.st_uid != os.getuid()
                or stat.S_IMODE(parent_descriptor.st_mode) != 0o700
                or _stable_identity(descriptor) != self.identity
                or _stable_identity(path_metadata) != self.identity
                or not stat.S_ISREG(descriptor.st_mode)
                or descriptor.st_uid != os.getuid()
                or descriptor.st_nlink != 1
                or stat.S_IMODE(descriptor.st_mode) != 0o600
                or descriptor.st_size != 0
            ):
                raise _frontend_error()
        except DeploymentError:
            raise
        except (OSError, TypeError, ValueError):
            raise _frontend_error() from None

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for field_name in ("fd", "parent_fd"):
            fd = getattr(self, field_name)
            setattr(self, field_name, -1)
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _open_frontend_userconfig(path: Path) -> _EmptyUserConfigBinding:
    parent_fd = fd = -1
    try:
        parent = path.parent
        if parent.resolve(strict=True) != parent:
            raise _frontend_error()
        parent_fd = os.open(
            parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        created = False
        try:
            fd = os.open(
                path.name,
                os.O_RDONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_CLOEXEC
                | os.O_NOFOLLOW
                | os.O_NONBLOCK,
                0o600,
                dir_fd=parent_fd,
            )
            created = True
        except FileExistsError:
            fd = os.open(
                path.name,
                os.O_RDONLY
                | os.O_CLOEXEC
                | os.O_NOFOLLOW
                | os.O_NONBLOCK,
                dir_fd=parent_fd,
            )
        if created:
            os.fchmod(fd, 0o600)
            os.fsync(fd)
            os.fsync(parent_fd)
        binding = _EmptyUserConfigBinding(
            path,
            parent_fd,
            fd,
            _stable_identity(os.fstat(parent_fd)),
            _stable_identity(os.fstat(fd)),
        )
        binding.verify()
        parent_fd = fd = -1
        return binding
    except DeploymentError:
        raise
    except (OSError, TypeError, ValueError):
        raise _frontend_error() from None
    finally:
        for candidate in (fd, parent_fd):
            if candidate >= 0:
                try:
                    os.close(candidate)
                except OSError:
                    pass


def _verify_frontend_private_directories(*paths: Path) -> None:
    for path in paths:
        fd = -1
        try:
            if path.resolve(strict=True) != path:
                raise _frontend_error()
            path_metadata = path.lstat()
            fd = os.open(
                path,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
            descriptor = os.fstat(fd)
            if (
                _stable_identity(descriptor) != _stable_identity(path_metadata)
                or not stat.S_ISDIR(descriptor.st_mode)
                or descriptor.st_uid != os.getuid()
                or stat.S_IMODE(descriptor.st_mode) != 0o700
            ):
                raise _frontend_error()
        except DeploymentError:
            raise
        except (OSError, TypeError, ValueError):
            raise _frontend_error() from None
        finally:
            if fd >= 0:
                os.close(fd)


def _capture_frontend_hooks(cmms_root: Path) -> bytes:
    arguments = (
        "config",
        "--includes",
        "--null",
        "--show-origin",
        "--show-scope",
        "--get-all",
        "core.hooksPath",
    )
    first = _git_bytes(
        cmms_root,
        arguments,
        limit=_GIT_METADATA_LIMIT,
        allowed_returncodes=(0, 1),
    )
    second = _git_bytes(
        cmms_root,
        arguments,
        limit=_GIT_METADATA_LIMIT,
        allowed_returncodes=(0, 1),
    )
    if first != second or (first and not first.endswith(b"\0")):
        raise _frontend_error()
    return first


def _verify_frontend_state(
    root: Path,
    cmms_root: Path,
    sensitive_path: Path,
    sensitive: SensitiveManifest,
    context: ClaimedApplyContext,
    lock: _OpenFrontendLock,
    expected_hooks: bytes,
    api_target: _GeneratedPathBinding,
) -> None:
    api_target.verify()
    if SourceInspector().capture(root, context.plan.profile) != context.plan.snapshot.source:
        raise _frontend_error()
    current_sensitive = SensitiveManifest.load(sensitive_path)
    if current_sensitive != sensitive:
        raise _frontend_error()
    baseline = verify_sensitive_baseline(cmms_root, sensitive)
    if (
        type(baseline) is not SensitiveBaselineResult
        or not baseline.ok
        or baseline.manifest_sha256 != sensitive.sha256
    ):
        raise _frontend_error()
    lock.verify()
    if _capture_frontend_hooks(cmms_root) != expected_hooks:
        raise _frontend_error()


def _maven_command(
    argv: tuple[str, ...],
    cwd: Path,
    environment: dict[str, str],
    safe_label: str,
) -> CommandSpec:
    return CommandSpec(
        argv=argv,
        cwd=cwd,
        environment=environment,
        timeout_seconds=1_800,
        stdout_limit=4 * 1024 * 1024,
        stderr_limit=4 * 1024 * 1024,
        safe_label=safe_label,
    )


class _GeneratedBindingChanged(Exception):
    pass


@dataclass
class _GeneratedScanBudget:
    entries: int = 0
    total_bytes: int = 0

    def claim(self, size: int) -> None:
        if (
            type(size) is not int
            or size < 0
            or size > _MAX_GENERATED_FILE_BYTES
            or self.entries >= _MAX_GENERATED_TREE_ENTRIES
            or self.total_bytes + size > _MAX_GENERATED_TOTAL_BYTES
        ):
            raise _GeneratedBindingChanged
        self.entries += 1
        self.total_bytes += size


@dataclass(frozen=True)
class _GeneratedTreeSample:
    rows: tuple[tuple[object, ...], ...]
    total_bytes: int


def _require_contained_generated_symlink(
    link_parts: tuple[str, ...],
    target: str,
) -> None:
    path = PurePosixPath(target)
    if not target or path.is_absolute():
        raise _GeneratedBindingChanged
    resolved = list(link_parts[:-1])
    for part in path.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not resolved:
                raise _GeneratedBindingChanged
            resolved.pop()
        else:
            resolved.append(part)


def _scan_generated_directory(
    directory_fd: int,
    prefix: tuple[str, ...],
    rows: list[tuple[object, ...]],
    budget: _GeneratedScanBudget,
    *,
    require_contained_symlinks: bool,
) -> None:
    try:
        before = os.fstat(directory_fd)
        if not stat.S_ISDIR(before.st_mode) or before.st_uid != os.getuid():
            raise _GeneratedBindingChanged
        names = sorted(os.listdir(directory_fd), key=os.fsencode)
        for name in names:
            if (
                type(name) is not str
                or name in {"", ".", ".."}
                or "/" in name
            ):
                raise _GeneratedBindingChanged
            name.encode("utf-8", errors="strict")
            parts = (*prefix, name)
            if len(parts) > _MAX_GENERATED_TREE_DEPTH:
                raise _GeneratedBindingChanged
            relative = "/".join(parts)
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if metadata.st_uid != os.getuid():
                raise _GeneratedBindingChanged
            if stat.S_ISDIR(metadata.st_mode):
                budget.claim(0)
                child_fd = os.open(
                    name,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_CLOEXEC
                    | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                try:
                    child_before = os.fstat(child_fd)
                    if _stable_identity(child_before) != _stable_identity(metadata):
                        raise _GeneratedBindingChanged
                    rows.append(
                        (relative, "directory", *_stable_identity(child_before))
                    )
                    _scan_generated_directory(
                        child_fd,
                        parts,
                        rows,
                        budget,
                        require_contained_symlinks=require_contained_symlinks,
                    )
                    child_after = os.fstat(child_fd)
                    child_path_after = os.stat(
                        name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                    if (
                        _stable_identity(child_before)
                        != _stable_identity(child_after)
                        or _stable_identity(child_before)
                        != _stable_identity(child_path_after)
                    ):
                        raise _GeneratedBindingChanged
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(metadata.st_mode):
                budget.claim(metadata.st_size)
                rows.append((relative, "regular", *_stable_identity(metadata)))
            elif stat.S_ISLNK(metadata.st_mode):
                target = os.readlink(name, dir_fd=directory_fd)
                encoded_target = os.fsencode(target)
                if len(encoded_target) > _MAX_GENERATED_SYMLINK_BYTES:
                    raise _GeneratedBindingChanged
                if require_contained_symlinks:
                    _require_contained_generated_symlink(parts, target)
                budget.claim(len(encoded_target))
                after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if (
                    _stable_identity(metadata) != _stable_identity(after)
                    or os.readlink(name, dir_fd=directory_fd) != target
                ):
                    raise _GeneratedBindingChanged
                rows.append(
                    (
                        relative,
                        "symlink",
                        encoded_target,
                        *_stable_identity(metadata),
                    )
                )
            else:
                raise _GeneratedBindingChanged
        after = os.fstat(directory_fd)
        if _stable_identity(before) != _stable_identity(after):
            raise _GeneratedBindingChanged
    except _GeneratedBindingChanged:
        raise
    except (OSError, RecursionError, TypeError, UnicodeError, ValueError):
        raise _GeneratedBindingChanged from None


def _sample_generated_directory_once(
    directory_fd: int,
    *,
    require_contained_symlinks: bool = True,
) -> _GeneratedTreeSample:
    rows: list[tuple[object, ...]] = []
    budget = _GeneratedScanBudget()
    _scan_generated_directory(
        directory_fd,
        (),
        rows,
        budget,
        require_contained_symlinks=require_contained_symlinks,
    )
    return _GeneratedTreeSample(tuple(rows), budget.total_bytes)


def _sample_generated_directory(
    directory_fd: int,
    *,
    require_contained_symlinks: bool = True,
) -> _GeneratedTreeSample:
    first = _sample_generated_directory_once(
        directory_fd,
        require_contained_symlinks=require_contained_symlinks,
    )
    second = _sample_generated_directory_once(
        directory_fd,
        require_contained_symlinks=require_contained_symlinks,
    )
    if first != second:
        raise _GeneratedBindingChanged
    return first


@dataclass
class _GeneratedPathBinding:
    path: Path
    expected_kind: str
    parent_fd: int = field(repr=False)
    parent_identity: tuple[int, ...] = field(repr=False)
    child_fd: int = field(repr=False)
    child_identity: tuple[int, ...] | None = field(repr=False)
    tree_sample: _GeneratedTreeSample | None = field(repr=False)
    failure: Callable[[], DeploymentError] = field(repr=False)
    closed: bool = field(default=False, init=False, repr=False)

    def verify(self) -> None:
        if self.closed or self.parent_fd < 0:
            raise self.failure()
        try:
            parent_descriptor = os.fstat(self.parent_fd)
            parent_path = self.path.parent.lstat()
            if (
                self.path.parent.resolve(strict=True) != self.path.parent
                or _stable_identity(parent_descriptor) != self.parent_identity
                or _stable_identity(parent_path) != self.parent_identity
                or not stat.S_ISDIR(parent_descriptor.st_mode)
                or parent_descriptor.st_uid != os.getuid()
            ):
                raise _GeneratedBindingChanged
            if self.child_identity is None:
                try:
                    os.stat(
                        self.path.name,
                        dir_fd=self.parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    return
                raise _GeneratedBindingChanged
            if self.child_fd < 0 or self.path.resolve(strict=True) != self.path:
                raise _GeneratedBindingChanged
            descriptor = os.fstat(self.child_fd)
            path_metadata = os.stat(
                self.path.name,
                dir_fd=self.parent_fd,
                follow_symlinks=False,
            )
            if (
                _stable_identity(descriptor) != self.child_identity
                or _stable_identity(path_metadata) != self.child_identity
                or descriptor.st_uid != os.getuid()
            ):
                raise _GeneratedBindingChanged
            if self.expected_kind == "directory":
                if (
                    not stat.S_ISDIR(descriptor.st_mode)
                    or self.tree_sample is None
                    or _sample_generated_directory(self.child_fd)
                    != self.tree_sample
                ):
                    raise _GeneratedBindingChanged
            elif (
                self.expected_kind != "regular"
                or not stat.S_ISREG(descriptor.st_mode)
                or descriptor.st_size > _MAX_GENERATED_FILE_BYTES
                or self.tree_sample is not None
            ):
                raise _GeneratedBindingChanged
        except DeploymentError:
            raise
        except _GeneratedBindingChanged:
            raise self.failure() from None
        except (OSError, RecursionError, TypeError, UnicodeError, ValueError):
            raise self.failure() from None

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for field_name in ("child_fd", "parent_fd"):
            fd = getattr(self, field_name)
            setattr(self, field_name, -1)
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _capture_generated_path(
    path: Path,
    expected_kind: str,
    failure: Callable[[], DeploymentError],
) -> _GeneratedPathBinding | _ExactIgnoredTreeBinding:
    parent_fd = child_fd = -1
    try:
        if expected_kind not in {"directory", "regular"}:
            raise failure()
        parent = path.parent
        try:
            resolved_parent = parent.resolve(strict=True)
        except FileNotFoundError:
            return _capture_missing_generated_path(path, expected_kind, failure)
        if resolved_parent != parent:
            raise failure()
        parent_fd = os.open(
            parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        parent_metadata = os.fstat(parent_fd)
        parent_path = parent.lstat()
        if (
            _stable_identity(parent_metadata) != _stable_identity(parent_path)
            or not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != os.getuid()
        ):
            raise failure()
        try:
            child_fd = os.open(
                path.name,
                os.O_RDONLY
                | os.O_CLOEXEC
                | os.O_NOFOLLOW
                | os.O_NONBLOCK,
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            binding = _GeneratedPathBinding(
                path,
                expected_kind,
                parent_fd,
                _stable_identity(parent_metadata),
                -1,
                None,
                None,
                failure,
            )
            binding.verify()
            parent_fd = -1
            return binding
        descriptor = os.fstat(child_fd)
        path_metadata = os.stat(
            path.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            path.resolve(strict=True) != path
            or _stable_identity(descriptor) != _stable_identity(path_metadata)
            or descriptor.st_uid != os.getuid()
            or (
                expected_kind == "directory"
                and not stat.S_ISDIR(descriptor.st_mode)
            )
            or (
                expected_kind == "regular"
                and (
                    not stat.S_ISREG(descriptor.st_mode)
                    or descriptor.st_size > _MAX_GENERATED_FILE_BYTES
                )
            )
        ):
            raise failure()
        sample = (
            _sample_generated_directory(child_fd)
            if expected_kind == "directory"
            else None
        )
        binding = _GeneratedPathBinding(
            path,
            expected_kind,
            parent_fd,
            _stable_identity(parent_metadata),
            child_fd,
            _stable_identity(descriptor),
            sample,
            failure,
        )
        binding.verify()
        parent_fd = child_fd = -1
        return binding
    except DeploymentError:
        raise
    except _GeneratedBindingChanged:
        raise failure() from None
    except (OSError, RecursionError, TypeError, UnicodeError, ValueError):
        raise failure() from None
    finally:
        for fd in (child_fd, parent_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


@dataclass
class _ExactIgnoredTreeBinding:
    path: Path
    anchor: Path
    anchor_fd: int = field(repr=False)
    anchor_identity: tuple[int, ...] = field(repr=False)
    child_fd: int = field(repr=False)
    child_identity: tuple[int, ...] | None = field(repr=False)
    tree_sample: _GeneratedTreeSample | None = field(repr=False)
    missing_name: str | None
    failure: Callable[[], DeploymentError] = field(repr=False)
    closed: bool = field(default=False, init=False, repr=False)

    def verify(self) -> None:
        if self.closed or self.anchor_fd < 0:
            raise self.failure()
        try:
            anchor_descriptor = os.fstat(self.anchor_fd)
            anchor_path = self.anchor.lstat()
            if (
                self.anchor.resolve(strict=True) != self.anchor
                or _stable_identity(anchor_descriptor) != self.anchor_identity
                or _stable_identity(anchor_path) != self.anchor_identity
                or not stat.S_ISDIR(anchor_descriptor.st_mode)
                or anchor_descriptor.st_uid != os.getuid()
            ):
                raise _GeneratedBindingChanged
            if self.child_identity is None:
                if self.child_fd >= 0 or self.missing_name is None:
                    raise _GeneratedBindingChanged
                try:
                    os.stat(
                        self.missing_name,
                        dir_fd=self.anchor_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    return
                raise _GeneratedBindingChanged
            if (
                self.child_fd < 0
                or self.missing_name is not None
                or self.path.resolve(strict=True) != self.path
                or self.tree_sample is None
            ):
                raise _GeneratedBindingChanged
            descriptor = os.fstat(self.child_fd)
            path_metadata = os.stat(
                self.path.name,
                dir_fd=self.anchor_fd,
                follow_symlinks=False,
            )
            if (
                _stable_identity(descriptor) != self.child_identity
                or _stable_identity(path_metadata) != self.child_identity
                or not stat.S_ISDIR(descriptor.st_mode)
                or descriptor.st_uid != os.getuid()
                or _sample_generated_directory(
                    self.child_fd,
                    require_contained_symlinks=False,
                )
                != self.tree_sample
            ):
                raise _GeneratedBindingChanged
        except DeploymentError:
            raise
        except _GeneratedBindingChanged:
            raise self.failure() from None
        except (OSError, RecursionError, TypeError, UnicodeError, ValueError):
            raise self.failure() from None

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for field_name in ("child_fd", "anchor_fd"):
            fd = getattr(self, field_name)
            setattr(self, field_name, -1)
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _capture_exact_ignored_tree(
    root: Path,
    relative: str,
    failure: Callable[[], DeploymentError],
) -> _ExactIgnoredTreeBinding:
    anchor_fd = child_fd = next_fd = -1
    try:
        parts = tuple(relative.split("/"))
        if (
            not parts
            or any(part in {"", ".", ".."} for part in parts)
            or root.resolve(strict=True) != root
        ):
            raise failure()
        anchor = root
        anchor_fd = os.open(
            anchor,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        anchor_metadata = os.fstat(anchor_fd)
        if (
            not stat.S_ISDIR(anchor_metadata.st_mode)
            or anchor_metadata.st_uid != os.getuid()
            or _stable_identity(anchor_metadata) != _stable_identity(anchor.lstat())
        ):
            raise failure()
        for index, name in enumerate(parts):
            try:
                next_fd = os.open(
                    name,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_CLOEXEC
                    | os.O_NOFOLLOW
                    | os.O_NONBLOCK,
                    dir_fd=anchor_fd,
                )
            except FileNotFoundError:
                binding = _ExactIgnoredTreeBinding(
                    root.joinpath(*parts),
                    anchor,
                    anchor_fd,
                    _stable_identity(os.fstat(anchor_fd)),
                    -1,
                    None,
                    None,
                    name,
                    failure,
                )
                binding.verify()
                anchor_fd = -1
                return binding
            next_metadata = os.fstat(next_fd)
            path_metadata = os.stat(
                name,
                dir_fd=anchor_fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISDIR(next_metadata.st_mode)
                or next_metadata.st_uid != os.getuid()
                or _stable_identity(next_metadata) != _stable_identity(path_metadata)
            ):
                raise failure()
            if index + 1 == len(parts):
                child_fd = next_fd
                next_fd = -1
                break
            os.close(anchor_fd)
            anchor_fd = next_fd
            next_fd = -1
            anchor = anchor / name
        target = root.joinpath(*parts)
        sample = _sample_generated_directory(
            child_fd,
            require_contained_symlinks=False,
        )
        binding = _ExactIgnoredTreeBinding(
            target,
            anchor,
            anchor_fd,
            _stable_identity(os.fstat(anchor_fd)),
            child_fd,
            _stable_identity(os.fstat(child_fd)),
            sample,
            None,
            failure,
        )
        binding.verify()
        anchor_fd = child_fd = -1
        return binding
    except DeploymentError:
        raise
    except _GeneratedBindingChanged:
        raise failure() from None
    except (OSError, RecursionError, TypeError, UnicodeError, ValueError):
        raise failure() from None
    finally:
        for fd in (next_fd, child_fd, anchor_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _capture_missing_generated_path(
    path: Path,
    expected_kind: str,
    failure: Callable[[], DeploymentError],
) -> _ExactIgnoredTreeBinding:
    if expected_kind not in {"directory", "regular"}:
        raise failure()
    anchor = path.parent
    remaining = [path.name]
    while True:
        try:
            if anchor.resolve(strict=True) != anchor:
                raise failure()
            break
        except FileNotFoundError:
            if anchor == anchor.parent:
                raise failure() from None
            remaining.insert(0, anchor.name)
            anchor = anchor.parent
    return _capture_exact_ignored_tree(anchor, "/".join(remaining), failure)


def _capture_root_ignored_trees(
    root: Path,
    failure: Callable[[], DeploymentError],
) -> list[_ExactIgnoredTreeBinding]:
    bindings: list[_ExactIgnoredTreeBinding] = []
    try:
        for raw_relative in sorted(
            _ROOT_APPROVED_GENERATED_DIRECTORIES - {b".runtime"}
        ):
            relative = raw_relative.decode("utf-8", errors="strict")
            bindings.append(
                _capture_exact_ignored_tree(root, relative, failure)
            )
        return bindings
    except Exception:
        for binding in reversed(bindings):
            binding.close()
        raise


@dataclass(frozen=True)
class _RuntimeOwnedRule:
    parts: tuple[str, ...]
    kind: str


@dataclass(frozen=True)
class _RuntimeBoundaryObservation:
    protected: _GeneratedTreeSample
    identities: tuple[tuple[tuple[str, ...], tuple[int, ...] | None], ...]


def _runtime_special_paths(
    rules: tuple[_RuntimeOwnedRule, ...],
) -> tuple[tuple[str, ...], ...]:
    paths: set[tuple[str, ...]] = set()
    for rule in rules:
        paths.update(
            rule.parts[:index]
            for index in range(1, len(rule.parts) + 1)
        )
    return tuple(sorted(paths))


def _scan_runtime_boundary_directory(
    directory_fd: int,
    prefix: tuple[str, ...],
    rows: list[tuple[object, ...]],
    identities: dict[tuple[str, ...], tuple[int, ...] | None],
    budget: _GeneratedScanBudget,
    rules: dict[tuple[str, ...], str],
    special_paths: set[tuple[str, ...]],
    mutable_content: set[tuple[str, ...]],
    contained_roots: set[tuple[str, ...]],
) -> None:
    try:
        before = os.fstat(directory_fd)
        if not stat.S_ISDIR(before.st_mode) or before.st_uid != os.getuid():
            raise _GeneratedBindingChanged
        for name in sorted(os.listdir(directory_fd), key=os.fsencode):
            if type(name) is not str or name in {"", ".", ".."} or "/" in name:
                raise _GeneratedBindingChanged
            name.encode("utf-8", errors="strict")
            parts = (*prefix, name)
            if len(parts) > _MAX_GENERATED_TREE_DEPTH:
                raise _GeneratedBindingChanged
            relative = "/".join(parts)
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if metadata.st_uid != os.getuid():
                raise _GeneratedBindingChanged
            if parts in special_paths:
                expected_kind = rules.get(parts)
                if expected_kind == "regular":
                    fd = os.open(
                        name,
                        os.O_RDONLY
                        | os.O_CLOEXEC
                        | os.O_NOFOLLOW
                        | os.O_NONBLOCK,
                        dir_fd=directory_fd,
                    )
                    try:
                        descriptor = os.fstat(fd)
                        if (
                            _stable_identity(descriptor)
                            != _stable_identity(metadata)
                            or not stat.S_ISREG(descriptor.st_mode)
                            or descriptor.st_nlink != 1
                            or descriptor.st_size != 0
                            or stat.S_IMODE(descriptor.st_mode) != 0o600
                        ):
                            raise _GeneratedBindingChanged
                        identities[parts] = _stable_identity(descriptor)
                    finally:
                        os.close(fd)
                    continue
                if not stat.S_ISDIR(metadata.st_mode):
                    raise _GeneratedBindingChanged
                child_fd = os.open(
                    name,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_CLOEXEC
                    | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                try:
                    child_before = os.fstat(child_fd)
                    if (
                        _stable_identity(child_before) != _stable_identity(metadata)
                        or child_before.st_uid != os.getuid()
                        or stat.S_IMODE(child_before.st_mode) != 0o700
                    ):
                        raise _GeneratedBindingChanged
                    identities[parts] = _private_directory_identity(child_before)
                    if parts not in mutable_content:
                        _scan_runtime_boundary_directory(
                            child_fd,
                            parts,
                            rows,
                            identities,
                            budget,
                            rules,
                            special_paths,
                            mutable_content,
                            contained_roots,
                        )
                    child_after = os.fstat(child_fd)
                    path_after = os.stat(
                        name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                    if (
                        _stable_identity(child_before)
                        != _stable_identity(child_after)
                        or _stable_identity(child_before)
                        != _stable_identity(path_after)
                    ):
                        raise _GeneratedBindingChanged
                finally:
                    os.close(child_fd)
                continue
            if stat.S_ISDIR(metadata.st_mode):
                budget.claim(0)
                child_fd = os.open(
                    name,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_CLOEXEC
                    | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                try:
                    child_before = os.fstat(child_fd)
                    if _stable_identity(child_before) != _stable_identity(metadata):
                        raise _GeneratedBindingChanged
                    rows.append(
                        (relative, "directory", *_stable_identity(child_before))
                    )
                    _scan_runtime_boundary_directory(
                        child_fd,
                        parts,
                        rows,
                        identities,
                        budget,
                        rules,
                        special_paths,
                        mutable_content,
                        contained_roots,
                    )
                    child_after = os.fstat(child_fd)
                    path_after = os.stat(
                        name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                    if (
                        _stable_identity(child_before)
                        != _stable_identity(child_after)
                        or _stable_identity(child_before)
                        != _stable_identity(path_after)
                    ):
                        raise _GeneratedBindingChanged
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(metadata.st_mode):
                if any(
                    parts[: len(cache_root)] == cache_root
                    for cache_root in contained_roots
                ) and metadata.st_nlink != 1:
                    raise _GeneratedBindingChanged
                budget.claim(metadata.st_size)
                rows.append((relative, "regular", *_stable_identity(metadata)))
            elif stat.S_ISLNK(metadata.st_mode):
                target = os.readlink(name, dir_fd=directory_fd)
                encoded_target = os.fsencode(target)
                if len(encoded_target) > _MAX_GENERATED_SYMLINK_BYTES:
                    raise _GeneratedBindingChanged
                for cache_root in contained_roots:
                    if parts[: len(cache_root)] == cache_root:
                        _require_contained_generated_symlink(
                            parts[len(cache_root) :],
                            target,
                        )
                budget.claim(len(encoded_target))
                after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if (
                    _stable_identity(metadata) != _stable_identity(after)
                    or os.readlink(name, dir_fd=directory_fd) != target
                ):
                    raise _GeneratedBindingChanged
                rows.append(
                    (relative, "symlink", encoded_target, *_stable_identity(metadata))
                )
            else:
                raise _GeneratedBindingChanged
        after = os.fstat(directory_fd)
        if _stable_identity(before) != _stable_identity(after):
            raise _GeneratedBindingChanged
    except _GeneratedBindingChanged:
        raise
    except (OSError, RecursionError, TypeError, UnicodeError, ValueError):
        raise _GeneratedBindingChanged from None


def _observe_runtime_boundary(
    runtime_fd: int,
    rules: tuple[_RuntimeOwnedRule, ...],
    mutable_content: set[tuple[str, ...]],
    contained_roots: set[tuple[str, ...]],
) -> _RuntimeBoundaryObservation:
    rule_map = {rule.parts: rule.kind for rule in rules}
    special = _runtime_special_paths(rules)

    def observe_once() -> _RuntimeBoundaryObservation:
        rows: list[tuple[object, ...]] = []
        identities: dict[tuple[str, ...], tuple[int, ...] | None] = {
            path: None for path in special
        }
        budget = _GeneratedScanBudget()
        _scan_runtime_boundary_directory(
            runtime_fd,
            (),
            rows,
            identities,
            budget,
            rule_map,
            set(special),
            mutable_content,
            contained_roots,
        )
        return _RuntimeBoundaryObservation(
            _GeneratedTreeSample(tuple(rows), budget.total_bytes),
            tuple(sorted(identities.items())),
        )

    first = observe_once()
    second = observe_once()
    if first != second:
        raise _GeneratedBindingChanged
    return first


@dataclass
class _RuntimeBoundaryBinding:
    path: Path
    fd: int = field(repr=False)
    root_identity: tuple[int, ...] = field(repr=False)
    rules: tuple[_RuntimeOwnedRule, ...]
    expected_identities: dict[tuple[str, ...], tuple[int, ...] | None] = field(
        repr=False
    )
    protected_sample: _GeneratedTreeSample = field(repr=False)
    mutable_content: set[tuple[str, ...]] = field(repr=False)
    creation_allowed: set[tuple[str, ...]] = field(repr=False)
    controlled_paths: set[tuple[str, ...]] = field(repr=False)
    contained_roots: set[tuple[str, ...]] = field(repr=False)
    failure: Callable[[], DeploymentError] = field(repr=False)
    closed: bool = field(default=False, init=False, repr=False)

    def _observe(self) -> _RuntimeBoundaryObservation:
        if self.closed or self.fd < 0:
            raise self.failure()
        descriptor = os.fstat(self.fd)
        path_metadata = self.path.lstat()
        if (
            self.path.resolve(strict=True) != self.path
            or _private_directory_identity(descriptor) != self.root_identity
            or _private_directory_identity(path_metadata) != self.root_identity
            or not stat.S_ISDIR(descriptor.st_mode)
            or descriptor.st_uid != os.getuid()
            or stat.S_IMODE(descriptor.st_mode) != 0o700
        ):
            raise _GeneratedBindingChanged
        return _observe_runtime_boundary(
            self.fd,
            self.rules,
            self.mutable_content,
            self.contained_roots,
        )

    def _require_observation(self) -> _RuntimeBoundaryObservation:
        observation = self._observe()
        if observation.protected != self.protected_sample:
            raise _GeneratedBindingChanged
        current = dict(observation.identities)
        for parts, expected in self.expected_identities.items():
            observed = current.get(parts)
            if expected is not None and observed != expected:
                raise _GeneratedBindingChanged
            if (
                expected is None
                and observed is not None
                and parts not in self.creation_allowed
            ):
                raise _GeneratedBindingChanged
        return observation

    def verify(self) -> None:
        try:
            self._require_observation()
        except DeploymentError:
            raise
        except _GeneratedBindingChanged:
            raise self.failure() from None
        except (OSError, RecursionError, TypeError, UnicodeError, ValueError):
            raise self.failure() from None

    def seal_owned_paths(self) -> None:
        try:
            observation = self._require_observation()
            current = dict(observation.identities)
            for parts in self.creation_allowed:
                if current.get(parts) is not None:
                    self.expected_identities[parts] = current[parts]
        except DeploymentError:
            raise
        except _GeneratedBindingChanged:
            raise self.failure() from None
        except (OSError, RecursionError, TypeError, UnicodeError, ValueError):
            raise self.failure() from None

    def release_controlled_path(self, parts: tuple[str, ...]) -> None:
        try:
            rule_map = {rule.parts: rule.kind for rule in self.rules}
            if (
                parts not in self.controlled_paths
                or parts in self.mutable_content
                or rule_map.get(parts) != "directory"
            ):
                raise _GeneratedBindingChanged
            self._require_observation()
            self.mutable_content.add(parts)
            self.creation_allowed.add(parts)
            observation = self._observe()
            self.protected_sample = observation.protected
        except DeploymentError:
            raise
        except _GeneratedBindingChanged:
            raise self.failure() from None
        except (OSError, RecursionError, TypeError, UnicodeError, ValueError):
            raise self.failure() from None

    def freeze_controlled_path(self, parts: tuple[str, ...]) -> None:
        try:
            if parts not in self.controlled_paths or parts not in self.mutable_content:
                raise _GeneratedBindingChanged
            self._require_observation()
            self.mutable_content.remove(parts)
            observation = self._observe()
            self.protected_sample = observation.protected
            current = dict(observation.identities)
            if current.get(parts) is None:
                raise _GeneratedBindingChanged
            self.expected_identities[parts] = current[parts]
            self._require_observation()
        except DeploymentError:
            raise
        except _GeneratedBindingChanged:
            raise self.failure() from None
        except (OSError, RecursionError, TypeError, UnicodeError, ValueError):
            raise self.failure() from None

    def allow_controlled_creation(self, parts: tuple[str, ...]) -> None:
        try:
            if parts not in self.controlled_paths or parts in self.creation_allowed:
                raise _GeneratedBindingChanged
            self._require_observation()
            self.creation_allowed.add(parts)
            self._require_observation()
        except DeploymentError:
            raise
        except _GeneratedBindingChanged:
            raise self.failure() from None
        except (OSError, RecursionError, TypeError, UnicodeError, ValueError):
            raise self.failure() from None

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        fd = self.fd
        self.fd = -1
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass


def _capture_runtime_boundary(
    runtime_root: Path,
    rules: tuple[_RuntimeOwnedRule, ...],
    *,
    mutable_content: set[tuple[str, ...]],
    creation_allowed: set[tuple[str, ...]],
    controlled_paths: set[tuple[str, ...]],
    contained_roots: set[tuple[str, ...]],
    failure: Callable[[], DeploymentError],
) -> _RuntimeBoundaryBinding:
    fd = -1
    try:
        if (
            not rules
            or len({rule.parts for rule in rules}) != len(rules)
            or any(
                not rule.parts
                or rule.kind not in {"directory", "regular"}
                or any(part in {"", ".", ".."} for part in rule.parts)
                for rule in rules
            )
            or runtime_root.resolve(strict=True) != runtime_root
            or not contained_roots.issubset({rule.parts for rule in rules})
        ):
            raise failure()
        fd = os.open(
            runtime_root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        metadata = os.fstat(fd)
        path_metadata = runtime_root.lstat()
        if (
            _private_directory_identity(metadata)
            != _private_directory_identity(path_metadata)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise failure()
        observation = _observe_runtime_boundary(
            fd,
            rules,
            mutable_content,
            contained_roots,
        )
        binding = _RuntimeBoundaryBinding(
            runtime_root,
            fd,
            _private_directory_identity(metadata),
            rules,
            dict(observation.identities),
            observation.protected,
            set(mutable_content),
            set(creation_allowed),
            set(controlled_paths),
            set(contained_roots),
            failure,
        )
        binding.verify()
        fd = -1
        return binding
    except DeploymentError:
        raise
    except _GeneratedBindingChanged:
        raise failure() from None
    except (OSError, RecursionError, TypeError, UnicodeError, ValueError):
        raise failure() from None
    finally:
        if fd >= 0:
            os.close(fd)


@dataclass(frozen=True)
class _TargetTreeSample:
    jars: tuple[str, ...]
    identities: tuple[tuple[object, ...], ...]


def _scan_target_directory(
    directory_fd: int,
    prefix: tuple[str, ...],
    rows: list[tuple[object, ...]],
    jars: list[str],
) -> None:
    try:
        directory_before = os.fstat(directory_fd)
        names = sorted(os.listdir(directory_fd), key=os.fsencode)
        for name in names:
            if (
                type(name) is not str
                or not name
                or name in {".", ".."}
                or "/" in name
                or len(rows) >= _MAX_API_TARGET_ENTRIES
            ):
                raise _build_error()
            relative_parts = (*prefix, name)
            relative = "/".join(relative_parts)
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                if len(relative_parts) > _MAX_API_TARGET_DEPTH:
                    raise _build_error()
                child_fd = os.open(
                    name,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_CLOEXEC
                    | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
                try:
                    child_before = os.fstat(child_fd)
                    if not _same_source_stat(metadata, child_before):
                        raise _build_error()
                    rows.append((relative, "directory", *_stable_identity(child_before)))
                    _scan_target_directory(child_fd, relative_parts, rows, jars)
                    child_after = os.fstat(child_fd)
                    child_path_after = os.stat(
                        name,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                    if (
                        not _same_source_stat(child_before, child_after)
                        or not _same_source_stat(child_before, child_path_after)
                    ):
                        raise _build_error()
                finally:
                    os.close(child_fd)
            elif stat.S_ISREG(metadata.st_mode):
                if metadata.st_nlink != 1:
                    raise _build_error()
                rows.append((relative, "regular", *_stable_identity(metadata)))
                if name.endswith(".jar"):
                    jars.append(relative)
            else:
                raise _build_error()
        directory_after = os.fstat(directory_fd)
        if not _same_source_stat(directory_before, directory_after):
            raise _build_error()
    except DeploymentError:
        raise
    except (OSError, RecursionError, TypeError, ValueError):
        raise _build_error() from None


def _same_source_stat(left: os.stat_result, right: os.stat_result) -> bool:
    return _stable_identity(left) == _stable_identity(right)


def _scan_target_tree(target_fd: int) -> _TargetTreeSample:
    rows: list[tuple[object, ...]] = []
    jars: list[str] = []
    _scan_target_directory(target_fd, (), rows, jars)
    return _TargetTreeSample(tuple(sorted(jars)), tuple(rows))


@dataclass(frozen=True)
class _PublishedArtifactBinding:
    sha256: str
    path: Path
    size: int
    identity: tuple[int, ...] = field(repr=False)
    builds_sample: _GeneratedTreeSample = field(repr=False)


def _digest_bound_fd(fd: int, size: int) -> str:
    observed = hashlib.sha256()
    offset = 0
    while offset < size:
        chunk = os.pread(fd, min(65_536, size - offset), offset)
        if not chunk:
            raise _build_error()
        observed.update(chunk)
        offset += len(chunk)
    if os.pread(fd, 1, size):
        raise _build_error()
    return observed.hexdigest()


def _verify_artifact_in_directory(
    directory_fd: int,
    name: str,
    digest: str,
    size: int,
    expected_identity: tuple[int, ...] | None = None,
    expected_inode: tuple[int, int] | None = None,
) -> tuple[int, ...]:
    fd = -1
    try:
        path_before = os.stat(
            name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(path_before.st_mode)
            or path_before.st_uid != os.getuid()
            or path_before.st_nlink != 1
            or path_before.st_size != size
            or size <= 0
            or size > _MAX_API_ARTIFACT_BYTES
            or stat.S_IMODE(path_before.st_mode) != 0o600
        ):
            raise _build_error()
        fd = os.open(
            name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
        before = os.fstat(fd)
        identity = _stable_identity(before)
        if (
            identity != _stable_identity(path_before)
            or (expected_identity is not None and identity != expected_identity)
            or (
                expected_inode is not None
                and (before.st_dev, before.st_ino) != expected_inode
            )
        ):
            raise _build_error()
        observed = _digest_bound_fd(fd, size)
        after = os.fstat(fd)
        path_after = os.stat(
            name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            observed != digest
            or _stable_identity(before) != _stable_identity(after)
            or _stable_identity(before) != _stable_identity(path_after)
        ):
            raise _build_error()
        return identity
    except DeploymentError:
        raise
    except (OSError, TypeError, ValueError):
        raise _build_error() from None
    finally:
        if fd >= 0:
            os.close(fd)


def _directory_binding(value: os.stat_result) -> tuple[int, ...]:
    return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_nlink)


@dataclass
class _PreparedApiCandidate:
    target: Path
    builds: Path
    target_fd: int = field(repr=False)
    source_fd: int = field(repr=False)
    builds_fd: int = field(repr=False)
    staging_fd: int = field(repr=False)
    target_identity: tuple[int, ...] = field(repr=False)
    target_sample: _TargetTreeSample = field(repr=False)
    source_identity: tuple[int, ...] = field(repr=False)
    builds_identity: tuple[int, ...] = field(repr=False)
    builds_binding: tuple[int, ...] = field(repr=False)
    builds_sample: _GeneratedTreeSample = field(repr=False)
    staging_identity: tuple[int, ...] = field(repr=False)
    sha256: str
    size: int
    closed: bool = field(default=False, init=False, repr=False)

    def _require_open(self) -> None:
        if self.closed or min(
            self.target_fd,
            self.source_fd,
            self.builds_fd,
            self.staging_fd,
        ) < 0:
            raise _build_error()

    def _require_target_binding(self) -> None:
        target_descriptor = os.fstat(self.target_fd)
        target_path = self.target.lstat()
        source_descriptor = os.fstat(self.source_fd)
        source_path = os.stat(
            "app.jar",
            dir_fd=self.target_fd,
            follow_symlinks=False,
        )
        if (
            self.target.resolve(strict=True) != self.target
            or _stable_identity(target_descriptor) != self.target_identity
            or _stable_identity(target_path) != self.target_identity
            or _stable_identity(source_descriptor) != self.source_identity
            or _stable_identity(source_path) != self.source_identity
            or _scan_target_tree(self.target_fd) != self.target_sample
        ):
            raise _build_error()

    def _require_builds_binding(self, *, exact: bool) -> None:
        descriptor = os.fstat(self.builds_fd)
        path_metadata = self.builds.lstat()
        if (
            self.builds.resolve(strict=True) != self.builds
            or _directory_binding(descriptor) != self.builds_binding
            or _directory_binding(path_metadata) != self.builds_binding
            or (exact and _stable_identity(descriptor) != self.builds_identity)
            or (exact and _stable_identity(path_metadata) != self.builds_identity)
            or (
                exact
                and _sample_generated_directory(self.builds_fd)
                != self.builds_sample
            )
        ):
            raise _build_error()

    def verify(self) -> None:
        try:
            self._require_open()
            self._require_target_binding()
            self._require_builds_binding(exact=True)
            staging_before = os.fstat(self.staging_fd)
            if (
                _stable_identity(staging_before) != self.staging_identity
                or not stat.S_ISREG(staging_before.st_mode)
                or staging_before.st_uid != os.getuid()
                or staging_before.st_nlink != 0
                or stat.S_IMODE(staging_before.st_mode) != 0o600
                or staging_before.st_size != self.size
                or _digest_bound_fd(self.staging_fd, self.size) != self.sha256
                or _stable_identity(os.fstat(self.staging_fd))
                != self.staging_identity
            ):
                raise _build_error()
            self._require_target_binding()
            self._require_builds_binding(exact=True)
        except DeploymentError:
            raise
        except (OSError, TypeError, ValueError):
            raise _build_error() from None

    def finalize(self) -> _PublishedArtifactBinding:
        try:
            self._require_open()
            self._require_builds_binding(exact=True)
            staging_before = os.fstat(self.staging_fd)
            if _stable_identity(staging_before) != self.staging_identity:
                raise _build_error()
            final_name = f"app-{self.sha256}.jar"
            final_path = self.builds / final_name
            linked = False
            try:
                os.link(
                    f"/proc/self/fd/{self.staging_fd}",
                    final_name,
                    dst_dir_fd=self.builds_fd,
                    follow_symlinks=True,
                )
                linked = True
            except FileExistsError:
                pass
            self._require_builds_binding(exact=False)
            expected_inode: tuple[int, int] | None = None
            if linked:
                staging_linked = os.fstat(self.staging_fd)
                expected_inode = (staging_before.st_dev, staging_before.st_ino)
                if (
                    (staging_linked.st_dev, staging_linked.st_ino)
                    != expected_inode
                    or not stat.S_ISREG(staging_linked.st_mode)
                    or staging_linked.st_uid != os.getuid()
                    or staging_linked.st_nlink != 1
                    or stat.S_IMODE(staging_linked.st_mode) != 0o600
                    or staging_linked.st_size != self.size
                ):
                    raise _build_error()
            final_identity = _verify_artifact_in_directory(
                self.builds_fd,
                final_name,
                self.sha256,
                self.size,
                expected_inode=expected_inode,
            )
            if linked:
                if _stable_identity(os.fstat(self.staging_fd)) != final_identity:
                    raise _build_error()
            os.fsync(self.builds_fd)
            final_identity = _verify_artifact_in_directory(
                self.builds_fd,
                final_name,
                self.sha256,
                self.size,
                expected_identity=final_identity,
                expected_inode=expected_inode,
            )
            self._require_builds_binding(exact=False)
            published_sample = _sample_generated_directory(self.builds_fd)
            if linked:
                final_rows = tuple(
                    row for row in published_sample.rows if row[0] == final_name
                )
                remaining_rows = tuple(
                    row for row in published_sample.rows if row[0] != final_name
                )
                if (
                    final_rows
                    != ((final_name, "regular", *final_identity),)
                    or remaining_rows != self.builds_sample.rows
                    or published_sample.total_bytes
                    != self.builds_sample.total_bytes + self.size
                ):
                    raise _build_error()
            elif published_sample != self.builds_sample:
                raise _build_error()
            return _PublishedArtifactBinding(
                self.sha256,
                final_path,
                self.size,
                final_identity,
                published_sample,
            )
        except DeploymentError:
            raise
        except (OSError, TypeError, ValueError):
            raise _build_error() from None

    def verify_published(self, binding: _PublishedArtifactBinding) -> None:
        try:
            self._require_open()
            final_name = f"app-{self.sha256}.jar"
            final_path = self.builds / final_name
            if (
                type(binding) is not _PublishedArtifactBinding
                or binding.sha256 != self.sha256
                or binding.path != final_path
                or binding.size != self.size
                or type(binding.identity) is not tuple
                or len(binding.identity) != 8
                or any(type(value) is not int for value in binding.identity)
                or type(binding.builds_sample) is not _GeneratedTreeSample
            ):
                raise _build_error()
            expected_inode = (binding.identity[0], binding.identity[1])
            self._require_builds_binding(exact=False)
            final_identity = _verify_artifact_in_directory(
                self.builds_fd,
                final_name,
                binding.sha256,
                binding.size,
                expected_identity=binding.identity,
                expected_inode=expected_inode,
            )
            self._require_builds_binding(exact=False)
            if _sample_generated_directory(self.builds_fd) != binding.builds_sample:
                raise _build_error()
            if final_identity != binding.identity:
                raise _build_error()
        except DeploymentError:
            raise
        except (OSError, TypeError, ValueError):
            raise _build_error() from None

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        for field_name in ("staging_fd", "source_fd", "target_fd", "builds_fd"):
            fd = getattr(self, field_name)
            setattr(self, field_name, -1)
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _prepare_api_candidate(target: Path, builds: Path) -> _PreparedApiCandidate:
    target_fd = source_fd = builds_fd = staging_fd = -1
    try:
        if target.resolve(strict=True) != target or not target.is_dir():
            raise _build_error()
        target_fd = os.open(
            target,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        target_before = os.fstat(target_fd)
        target_path_before = target.lstat()
        if (
            not stat.S_ISDIR(target_before.st_mode)
            or _stable_identity(target_before) != _stable_identity(target_path_before)
        ):
            raise _build_error()
        target_sample = _scan_target_tree(target_fd)
        if target_sample.jars != ("app.jar",):
            raise _build_error()
        source_fd = os.open(
            "app.jar",
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=target_fd,
        )
        source_before = os.fstat(source_fd)
        source_path_before = os.stat(
            "app.jar",
            dir_fd=target_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(source_before.st_mode)
            or source_before.st_uid != os.getuid()
            or source_before.st_nlink != 1
            or source_before.st_size <= 0
            or source_before.st_size > _MAX_API_ARTIFACT_BYTES
            or _stable_identity(source_before) != _stable_identity(source_path_before)
        ):
            raise _build_error()
        builds = _ensure_private_directory(builds)
        builds_fd = os.open(
            builds,
            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        tmpfile_flag = getattr(os, "O_TMPFILE", None)
        if type(tmpfile_flag) is not int:
            raise _build_error()
        staging_fd = os.open(
            ".",
            os.O_RDWR | os.O_CLOEXEC | tmpfile_flag,
            0o600,
            dir_fd=builds_fd,
        )
        os.fchmod(staging_fd, 0o600)
        staging_empty = os.fstat(staging_fd)
        if (
            not stat.S_ISREG(staging_empty.st_mode)
            or staging_empty.st_uid != os.getuid()
            or staging_empty.st_nlink != 0
            or stat.S_IMODE(staging_empty.st_mode) != 0o600
            or staging_empty.st_size != 0
        ):
            raise _build_error()
        digest = hashlib.sha256()
        offset = 0
        while offset < source_before.st_size:
            chunk = os.pread(
                source_fd,
                min(65_536, source_before.st_size - offset),
                offset,
            )
            if not chunk:
                raise _build_error()
            digest.update(chunk)
            written = 0
            while written < len(chunk):
                count = os.pwrite(staging_fd, chunk[written:], offset + written)
                if count <= 0:
                    raise _build_error()
                written += count
            offset += len(chunk)
        if os.pread(source_fd, 1, source_before.st_size):
            raise _build_error()
        os.fsync(staging_fd)
        source_after = os.fstat(source_fd)
        source_path_after = os.stat(
            "app.jar",
            dir_fd=target_fd,
            follow_symlinks=False,
        )
        target_after = os.fstat(target_fd)
        target_path_after = target.lstat()
        staging_after = os.fstat(staging_fd)
        builds_after = os.fstat(builds_fd)
        builds_path_after = builds.lstat()
        builds_sample = _sample_generated_directory(builds_fd)
        builds_final = os.fstat(builds_fd)
        builds_path_final = builds.lstat()
        if (
            _stable_identity(source_before) != _stable_identity(source_after)
            or _stable_identity(source_before) != _stable_identity(source_path_after)
            or _stable_identity(target_before) != _stable_identity(target_after)
            or _stable_identity(target_before) != _stable_identity(target_path_after)
            or _scan_target_tree(target_fd) != target_sample
            or not stat.S_ISREG(staging_after.st_mode)
            or staging_after.st_uid != os.getuid()
            or staging_after.st_nlink != 0
            or stat.S_IMODE(staging_after.st_mode) != 0o600
            or staging_after.st_size != source_before.st_size
            or _digest_bound_fd(staging_fd, source_before.st_size)
            != digest.hexdigest()
            or _stable_identity(os.fstat(staging_fd))
            != _stable_identity(staging_after)
            or not stat.S_ISDIR(builds_after.st_mode)
            or builds_after.st_uid != os.getuid()
            or stat.S_IMODE(builds_after.st_mode) != 0o700
            or _stable_identity(builds_after)
            != _stable_identity(builds_path_after)
            or _directory_binding(builds_after)
            != _directory_binding(builds_path_after)
            or _stable_identity(builds_after) != _stable_identity(builds_final)
            or _stable_identity(builds_after)
            != _stable_identity(builds_path_final)
        ):
            raise _build_error()
        prepared = _PreparedApiCandidate(
            target,
            builds,
            target_fd,
            source_fd,
            builds_fd,
            staging_fd,
            _stable_identity(target_before),
            target_sample,
            _stable_identity(source_before),
            _stable_identity(builds_after),
            _directory_binding(builds_after),
            builds_sample,
            _stable_identity(staging_after),
            digest.hexdigest(),
            source_before.st_size,
        )
        target_fd = source_fd = builds_fd = staging_fd = -1
        return prepared
    except DeploymentError:
        raise
    except (OSError, TypeError, ValueError):
        raise _build_error() from None
    finally:
        for fd in (staging_fd, source_fd, target_fd, builds_fd):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
