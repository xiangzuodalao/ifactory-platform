"""Records-only CLI boundary; public deployment commands remain disabled."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


_TASK2_UNAVAILABLE_COMMANDS = frozenset(
    {"status", "secret", "plan", "apply", "internal"}
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cmms-development",
        description="Fail-closed CMMS development deployment control",
    )
    parser.add_argument("command", nargs="?")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments, unknown = parser.parse_known_args(argv)
    if arguments.command is None and not unknown:
        parser.print_help()
        return 0
    # Keep the frozen command vocabulary visible without enabling an incomplete
    # planner or apply path. Unknown commands intentionally share the safe code.
    if arguments.command in _TASK2_UNAVAILABLE_COMMANDS:
        print("CMMS-E020 command-not-available", file=sys.stderr)
        return 20
    print("CMMS-E020 command-not-available", file=sys.stderr)
    return 20
