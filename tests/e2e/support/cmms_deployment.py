"""Test-only fakes and private runtime-file factories for CMMS deployment."""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Collection


@dataclass(frozen=True)
class SafeRuntimeFixture:
    root: Path

    def _safe_path(self, relative: str | Path) -> Path:
        raw = os.fspath(relative)
        components = raw.split(os.sep)
        candidate = Path(raw)
        if (
            not raw
            or candidate.is_absolute()
            or any(component in {"", ".", ".."} for component in components)
        ):
            raise ValueError("unsafe test fixture path")
        root = Path(os.path.abspath(self.root))
        path = Path(os.path.abspath(root / candidate))
        if path == root or not path.is_relative_to(root):
            raise ValueError("unsafe test fixture path")
        return path

    def private_directory(self, relative: str | Path) -> Path:
        path = self._safe_path(relative)
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        path.chmod(0o700)
        return path

    def private_file(self, relative: str | Path, data: bytes) -> Path:
        path = self._safe_path(relative)
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        path.parent.chmod(0o700)
        path.write_bytes(data)
        path.chmod(0o600)
        return path


@dataclass(frozen=True)
class CmmsSourceFixture:
    root: Path
    head: str
    dirty_paths: tuple[str, ...] = ()

    @property
    def is_clean(self) -> bool:
        return not self.dirty_paths


@dataclass
class ScriptedRunner:
    outcomes: deque[Any] = field(default_factory=deque)
    calls: list[Any] = field(default_factory=list)

    @classmethod
    def from_outcomes(cls, outcomes: Collection[Any]) -> ScriptedRunner:
        return cls(deque(outcomes))

    def run(self, spec: Any) -> Any:
        self.calls.append(spec)
        if not self.outcomes:
            raise AssertionError("unexpected process invocation")
        outcome = self.outcomes.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@dataclass
class ScriptedHttpTransport:
    outcomes: deque[Any] = field(default_factory=deque)
    calls: list[tuple[str, str, bytes | None, dict[str, str]]] = field(
        default_factory=list
    )

    @classmethod
    def from_outcomes(cls, outcomes: Collection[Any]) -> ScriptedHttpTransport:
        return cls(deque(outcomes))

    def request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        self.calls.append((method, url, body, dict(headers or {})))
        if not self.outcomes:
            raise AssertionError("unexpected HTTP invocation")
        outcome = self.outcomes.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def make_private_file(path: Path, data: bytes) -> Path:
    """Small compatibility helper for tests that do not need a fixture object."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, 0o600)
    return path
