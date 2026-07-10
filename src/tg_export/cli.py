"""Command-line entry point for tg-export.

tg-export is a tdl-raw -> contract transformer (ADR-0011): it consumes a tdl export
and writes the msgbrowse JSON archive. There is one command, ``transform``, and it
is fully non-interactive — auth and extraction are tdl's job against an installed
Telegram Desktop session, exactly as msgbrowse defers Signal/iMessage/WhatsApp auth
to those source apps. Domain failures raise the sentinels in
:mod:`tg_export.errors`; :func:`main` maps each to its dedicated exit code.

# Governing: SPEC-0001 REQ "CLI Surface", REQ "Error Handling Standards"; ADR-0011
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__, transform
from .errors import EXIT_MALFORMED_ARG, EXIT_OK, EXIT_RUNTIME, TgExportError
from .logging import configure as configure_logging
from .logging import log_event


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tg-export",
        description="Transform a tdl Telegram export into an ingestion-ready JSON "
        "archive for msgbrowse.",
    )
    parser.add_argument("--version", action="version", version=__version__)

    sub = parser.add_subparsers(dest="command")

    p_transform = sub.add_parser(
        "transform", help="transform a tdl export into the msgbrowse JSON archive"
    )
    p_transform.add_argument(
        "--input", required=True, help="path to the tdl export (file or directory)"
    )
    p_transform.add_argument(
        "--output", required=True, help="output archive directory to write"
    )
    p_transform.add_argument(
        "--json-logs",
        action="store_true",
        help="emit progress/errors as one JSON object per line (machine-ingestible)",
    )

    return parser


def _cmd_transform(args: argparse.Namespace) -> int:
    configure_logging(json_logs=args.json_logs)
    config = transform.TransformConfig(
        input=Path(args.input),
        output=Path(args.output),
    )
    manifest = transform.run_transform(config)
    total = sum(chat["message_count"] for chat in manifest["chats"])
    # Summary carries only counts and the output path — never a message body.
    print(
        f"tg-export: transformed {len(manifest['chats'])} chats, {total} messages "
        f"to {args.output}",
        file=sys.stderr,
    )
    return EXIT_OK


_COMMANDS = {
    "transform": _cmd_transform,
}


def _dispatch(args: argparse.Namespace) -> int:
    handler = _COMMANDS[args.command]
    return handler(args)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse: --version/-h exit 0, usage errors exit 2
        code = exc.code
        return code if isinstance(code, int) else EXIT_MALFORMED_ARG

    if args.command is None:
        parser.print_help(sys.stderr)
        return EXIT_MALFORMED_ARG

    try:
        return _dispatch(args)
    except TgExportError as exc:
        print(str(exc), file=sys.stderr)
        log_event("error", level=40, command=args.command, code=exc.exit_code, **exc.context)
        return exc.exit_code
    except Exception:  # noqa: BLE001 - last-resort guard: map to generic runtime code
        print("tg-export: unexpected runtime error", file=sys.stderr)
        log_event("unexpected_error", level=40, command=args.command)
        return EXIT_RUNTIME


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
