"""Bounded subprocess execution with an explicit, non-ambient environment."""

from __future__ import annotations

import math
import os
import selectors
import signal
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from .errors import DeploymentError


class ProcessDeadlineExceeded(RuntimeError):
    pass


class ProcessOutputLimitExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandSpec:
    argv: tuple[str, ...] = field(repr=False)
    cwd: Path
    environment: Mapping[str, str] = field(repr=False)
    timeout_seconds: float
    stdout_limit: int
    stderr_limit: int
    safe_label: str
    input_bytes: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "argv", tuple(self.argv))
        object.__setattr__(
            self,
            "environment",
            MappingProxyType(dict(self.environment)),
        )
        object.__setattr__(self, "cwd", Path(self.cwd))


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


def _unsafe_command() -> DeploymentError:
    return DeploymentError("CMMS-E003", "unsafe command specification", 3)


def _validate_spec(spec: CommandSpec) -> None:
    if (
        not spec.argv
        or any(
            type(value) is not str or not value or "\x00" in value
            for value in spec.argv
        )
        or not Path(spec.argv[0]).is_absolute()
        or not spec.cwd.is_absolute()
        or not spec.cwd.is_dir()
    ):
        raise _unsafe_command()
    if (
        not isinstance(spec.timeout_seconds, (int, float))
        or isinstance(spec.timeout_seconds, bool)
        or not math.isfinite(spec.timeout_seconds)
        or spec.timeout_seconds <= 0
        or type(spec.stdout_limit) is not int
        or spec.stdout_limit < 0
        or type(spec.stderr_limit) is not int
        or spec.stderr_limit < 0
    ):
        raise _unsafe_command()
    if (
        type(spec.safe_label) is not str
        or not spec.safe_label
        or len(spec.safe_label) > 128
        or not spec.safe_label.isprintable()
        or "/" in spec.safe_label
        or "\\" in spec.safe_label
    ):
        raise _unsafe_command()
    if spec.input_bytes is not None and type(spec.input_bytes) is not bytes:
        raise _unsafe_command()
    for key, value in spec.environment.items():
        if (
            type(key) is not str
            or type(value) is not str
            or not key
            or "=" in key
            or "\x00" in key
            or "\x00" in value
        ):
            raise _unsafe_command()


def _close_stream(selector: selectors.BaseSelector, stream: object) -> None:
    try:
        selector.unregister(stream)
    except (KeyError, ValueError):
        pass
    try:
        stream.close()  # type: ignore[attr-defined]
    except OSError:
        pass


def collect_bounded_process_output(
    process: subprocess.Popen[bytes],
    *,
    input_bytes: bytes | None,
    deadline: float,
    stdout_limit: int,
    stderr_limit: int,
) -> tuple[bytes, bytes]:
    if process.stdout is None or process.stderr is None:
        raise _unsafe_command()
    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr = bytearray()
    input_offset = 0
    streams: dict[object, tuple[str, bytearray | None, int]] = {
        process.stdout: ("read", stdout, stdout_limit),
        process.stderr: ("read", stderr, stderr_limit),
    }
    try:
        for stream in (process.stdout, process.stderr):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ)
        if process.stdin is not None:
            os.set_blocking(process.stdin.fileno(), False)
            streams[process.stdin] = ("write", None, 0)
            selector.register(process.stdin, selectors.EVENT_WRITE)

        while selector.get_map():
            now = time.monotonic()
            if now >= deadline:
                raise ProcessDeadlineExceeded
            events = selector.select(deadline - now)
            if not events:
                raise ProcessDeadlineExceeded
            for key, _mask in events:
                stream = key.fileobj
                operation, buffer, limit = streams[stream]
                if operation == "write":
                    payload = input_bytes or b""
                    if input_offset >= len(payload):
                        _close_stream(selector, stream)
                        continue
                    try:
                        written = os.write(
                            stream.fileno(),  # type: ignore[attr-defined]
                            payload[input_offset : input_offset + 65_536],
                        )
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        _close_stream(selector, stream)
                        continue
                    input_offset += written
                    if input_offset >= len(payload):
                        _close_stream(selector, stream)
                    continue

                assert buffer is not None
                try:
                    chunk = os.read(
                        stream.fileno(),  # type: ignore[attr-defined]
                        min(65_536, limit - len(buffer) + 1),
                    )
                except BlockingIOError:
                    continue
                if not chunk:
                    _close_stream(selector, stream)
                    continue
                buffer.extend(chunk)
                if len(buffer) > limit:
                    raise ProcessOutputLimitExceeded
        return bytes(stdout), bytes(stderr)
    finally:
        selector.close()


def _close_process_streams(process: subprocess.Popen[bytes]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


class CommandRunner:
    def run(self, spec: CommandSpec) -> CommandResult:
        _validate_spec(spec)
        try:
            process = subprocess.Popen(
                spec.argv,
                cwd=spec.cwd,
                env=dict(spec.environment),
                stdin=(
                    subprocess.PIPE
                    if spec.input_bytes is not None
                    else subprocess.DEVNULL
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
            )
        except (OSError, ValueError):
            raise _unsafe_command() from None
        deadline = time.monotonic() + spec.timeout_seconds
        try:
            stdout, stderr = collect_bounded_process_output(
                process,
                input_bytes=spec.input_bytes,
                deadline=deadline,
                stdout_limit=spec.stdout_limit,
                stderr_limit=spec.stderr_limit,
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProcessDeadlineExceeded
            returncode = process.wait(timeout=remaining)
        except (
            ProcessDeadlineExceeded,
            ProcessOutputLimitExceeded,
            subprocess.TimeoutExpired,
        ):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            _close_process_streams(process)
            raise DeploymentError(
                "CMMS-E005",
                f"{spec.safe_label} did not complete safely",
                5,
            ) from None
        _close_process_streams(process)
        if returncode != 0:
            raise DeploymentError(
                "CMMS-E004",
                f"{spec.safe_label} failed",
                4,
            )
        return CommandResult(
            returncode=returncode,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )
