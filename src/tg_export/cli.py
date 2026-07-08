"""Command-line entry point for tg-export.

M1 scaffolds only the argument surface and ``--version`` (the version signal that
``msgbrowse doctor`` reads). The full command implementations — ``login``,
``export``, ``chats``, ``doctor`` — and their sentinel exit codes land in later
milestones (M2+). This stub keeps the installed ``tg-export`` console script
importable and makes ``--version`` work end-to-end from a built wheel.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import __version__

# Governing: SPEC-0001 REQ "CLI Surface" (scaffold only in M1; full surface in M2+)

_NOT_IMPLEMENTED_EXIT = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tg-export",
        description="Export a Telegram account's history into an ingestion-ready "
        "JSON archive for msgbrowse.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    # Subcommands (login/export/chats/doctor) are added in later milestones.
    parser.add_argument(
        "command",
        nargs="?",
        choices=["login", "export", "chats", "doctor"],
        help="command to run (implemented in later milestones)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help(sys.stderr)
        return _NOT_IMPLEMENTED_EXIT

    print(
        f"tg-export: command '{args.command}' is not implemented yet "
        "(scaffold milestone M1)",
        file=sys.stderr,
    )
    return _NOT_IMPLEMENTED_EXIT


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
