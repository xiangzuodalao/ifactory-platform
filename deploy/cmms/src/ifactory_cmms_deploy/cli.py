"""Initial stable CLI boundary; later tasks add one command slice at a time."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cmms-development",
        description="Fail-closed CMMS development deployment control",
    )
    parser.add_argument("command", nargs="?")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments, _unknown = parser.parse_known_args(argv)
    if arguments.command is None:
        parser.print_help()
        return 0
    print("CMMS-E020 command-not-available", file=sys.stderr)
    return 20
