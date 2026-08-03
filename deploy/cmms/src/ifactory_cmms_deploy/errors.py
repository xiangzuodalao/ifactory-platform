"""Stable, operator-safe deployment failures."""

from __future__ import annotations


class DeploymentError(RuntimeError):
    """A failure containing only a stable code and a redacted message."""

    def __init__(self, code: str, safe_message: str, exit_code: int) -> None:
        self.code = code
        self.safe_message = safe_message
        self.exit_code = exit_code
        super().__init__(f"{code} {safe_message}")
