#!/usr/bin/env python3
"""Hold the host operation lock across one closed-loop wrapper invocation."""

from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import stat
import sys


LOCK_NAME = ".operation.lock"
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class OperationLockError(RuntimeError):
    pass


def _directory(path: Path) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError:
        raise OperationLockError("OPERATION_LOCK_DIRECTORY_INVALID") from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or metadata.st_uid != os.getuid()
        or metadata.st_gid != os.getgid()
        or path.is_symlink()
    ):
        raise OperationLockError("OPERATION_LOCK_DIRECTORY_INVALID")


def _validate_descriptor(descriptor: int, path: Path) -> None:
    try:
        metadata = os.fstat(descriptor)
        path_metadata = path.stat(follow_symlinks=False)
    except OSError:
        raise OperationLockError("OPERATION_LOCK_INVALID") from None
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.getuid()
        or metadata.st_gid != os.getgid()
        or metadata.st_nlink != 1
        or path.is_symlink()
        or (metadata.st_dev, metadata.st_ino) != (path_metadata.st_dev, path_metadata.st_ino)
    ):
        raise OperationLockError("OPERATION_LOCK_INVALID")


def _lock_path(control: Path) -> Path:
    _directory(control)
    return control / LOCK_NAME


def verify(control: Path, descriptor: int) -> None:
    path = _lock_path(control)
    _validate_descriptor(descriptor, path)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise OperationLockError("OPERATION_LOCK_INVALID") from None


def execute(control: Path, command: list[str]) -> None:
    if not command or not Path(command[0]).is_absolute():
        raise OperationLockError("OPERATION_COMMAND_INVALID")
    path = _lock_path(control)
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o600,
        )
    except OSError:
        raise OperationLockError("OPERATION_LOCK_INVALID") from None
    _validate_descriptor(descriptor, path)
    try:
        directory = os.open(
            path.parent,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        os.set_inheritable(descriptor, True)
        os.execve(
            command[0],
            command,
            {
                "PATH": SAFE_PATH,
                "IFACTORY_PILOT_OPERATION_LOCK_FD": str(descriptor),
            },
        )
    except OSError:
        os.close(descriptor)
        raise OperationLockError("OPERATION_LOCK_FAILED") from None


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--control", type=Path, required=True)
    verify_parser.add_argument("--fd", type=int, required=True)
    execute_parser = subparsers.add_parser("execute")
    execute_parser.add_argument("--control", type=Path, required=True)
    execute_parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        if args.action == "verify":
            verify(args.control, args.fd)
        else:
            command = args.command[1:] if args.command[:1] == ["--"] else args.command
            execute(args.control, command)
        return 0
    except OperationLockError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
