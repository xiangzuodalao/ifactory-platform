#!/usr/bin/env python3
"""Durable, cooperative state locking for closed-loop pilot control scripts."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import tempfile
from typing import Iterator


class StateIoError(RuntimeError):
    pass


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise StateIoError("STATE_DIRECTORY_SYNC_FAILED") from None
    try:
        os.fsync(descriptor)
    except OSError:
        raise StateIoError("STATE_DIRECTORY_SYNC_FAILED") from None
    finally:
        os.close(descriptor)


def prepare_atomic_replacement(target: Path, content: bytes, *, prefix: str) -> Path:
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=prefix, dir=target.parent)
    except OSError:
        raise StateIoError("STATE_ATOMIC_WRITE_FAILED") from None
    path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise OSError("short write")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        fsync_directory(target.parent)
        return path
    except (OSError, StateIoError):
        if descriptor >= 0:
            os.close(descriptor)
        try:
            path.unlink()
        except OSError:
            pass
        raise StateIoError("STATE_ATOMIC_WRITE_FAILED") from None


def commit_atomic_replacement(temporary: Path, target: Path) -> None:
    try:
        os.replace(temporary, target)
        fsync_directory(target.parent)
    except (OSError, StateIoError):
        raise StateIoError("STATE_ATOMIC_REPLACE_FAILED") from None


def atomic_replace(target: Path, content: bytes, *, prefix: str) -> None:
    temporary = prepare_atomic_replacement(target, content, prefix=prefix)
    try:
        commit_atomic_replacement(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def exclusive_state_lock(path: Path) -> Iterator[None]:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise StateIoError("STATE_LOCK_INVALID") from None
    try:
        metadata = os.fstat(descriptor)
        path_metadata = path.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or metadata.st_gid != os.getgid()
            or metadata.st_nlink != 1
            or path.is_symlink()
            or (metadata.st_dev, metadata.st_ino) != (path_metadata.st_dev, path_metadata.st_ino)
        ):
            raise StateIoError("STATE_LOCK_INVALID")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except OSError:
        raise StateIoError("STATE_LOCK_FAILED") from None
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
