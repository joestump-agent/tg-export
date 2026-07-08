"""Command-line entry point for tg-export.

M2 lands the auth surface: ``login`` (one-time interactive), ``doctor`` (headless
authorization check), credential resolution, and the sentinel exit-code model.
``export`` and ``chats`` resolve credentials through the same plumbing but their
full history logic is M3 — they exit non-zero with a clear "not implemented yet"
message rather than silently claiming success. ``--version`` (M1) is unchanged.

Every command except ``login`` is non-interactive (SPEC-0001 REQ "CLI Surface").
Domain failures raise the sentinels in :mod:`tg_export.errors`; :func:`main` maps
each to its dedicated exit code and prints the stable ``tg-export: not authorized``
token for the unauthorized case.

# Governing: SPEC-0001 REQ "CLI Surface", REQ "Authentication and Session Model",
#            REQ "Error Handling Standards"; ADR-0009
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import __version__, auth
from .errors import (
    EXIT_MALFORMED_ARG,
    EXIT_OK,
    EXIT_RUNTIME,
    NOT_AUTHORIZED_TOKEN,
    MalformedArgumentError,
    NotAuthorizedError,
    TgExportError,
)
from .logging import log_event


def _add_credential_args(sub: argparse.ArgumentParser) -> None:
    # Per-user credential (ADR-0006): flags override the TG_EXPORT_API_* env vars.
    sub.add_argument("--api-id", default=None, help="Telegram API id (or TG_EXPORT_API_ID)")
    sub.add_argument(
        "--api-hash", default=None, help="Telegram API hash (or TG_EXPORT_API_HASH)"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tg-export",
        description="Export a Telegram account's history into an ingestion-ready "
        "JSON archive for msgbrowse.",
    )
    parser.add_argument("--version", action="version", version=__version__)

    sub = parser.add_subparsers(dest="command")

    # login: the only interactive command.
    p_login = sub.add_parser("login", help="one-time interactive login; writes a session")
    p_login.add_argument("--session", required=True, help="path to the session file (caller-owned)")
    _add_credential_args(p_login)

    # doctor: headless session validity check.
    p_doctor = sub.add_parser("doctor", help="check that a session is valid and authorized")
    p_doctor.add_argument("--session", required=True, help="path to the session file")
    _add_credential_args(p_doctor)

    # export: full behavior is M3; surface is defined here.
    p_export = sub.add_parser("export", help="export account history (M3)")
    p_export.add_argument("--session", required=True, help="path to the session file")
    p_export.add_argument("--output", help="output archive directory")
    p_export.add_argument("--since", help="prior export directory for an incremental run")
    p_export.add_argument("--full", action="store_true", help="ignore anchors; re-export all")
    p_export.add_argument("--chats", help="comma-separated chat ids to limit the export")
    p_export.add_argument("--no-media", action="store_true", help="export metadata only")
    p_export.add_argument("--max-media-mb", type=int, help="skip media larger than N MB")
    _add_credential_args(p_export)

    # chats: full behavior is M3; surface is defined here.
    p_chats = sub.add_parser("chats", help="list chats (M3)")
    p_chats.add_argument("--session", required=True, help="path to the session file")
    p_chats.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    _add_credential_args(p_chats)

    return parser


def _cmd_login(args: argparse.Namespace) -> int:
    credential = auth.resolve_credential(args.api_id, args.api_hash)
    resolved = auth.login(session=args.session, credential=credential)
    # Confirmation carries only the path — no secret, no session blob.
    print(f"tg-export: session written to {resolved}", file=sys.stderr)
    return EXIT_OK


def _cmd_doctor(args: argparse.Namespace) -> int:
    credential = auth.resolve_credential(args.api_id, args.api_hash)
    auth.verify_session(session=args.session, credential=credential)
    log_event("doctor_ok", session=str(args.session))
    print("tg-export: session is authorized", file=sys.stderr)
    return EXIT_OK


def _cmd_export(args: argparse.Namespace) -> int:
    # Resolve credentials through the M2 plumbing so a misconfigured invocation
    # fails the same way it will in M3; the history walk itself lands in M3.
    auth.resolve_credential(args.api_id, args.api_hash)
    print("tg-export: export is not implemented until M3", file=sys.stderr)
    return EXIT_RUNTIME


def _cmd_chats(args: argparse.Namespace) -> int:
    auth.resolve_credential(args.api_id, args.api_hash)
    print("tg-export: chats is not implemented until M3", file=sys.stderr)
    return EXIT_RUNTIME


_COMMANDS = {
    "login": _cmd_login,
    "doctor": _cmd_doctor,
    "export": _cmd_export,
    "chats": _cmd_chats,
}


def _dispatch(args: argparse.Namespace) -> int:
    handler = _COMMANDS.get(args.command)
    if handler is None:  # pragma: no cover - argparse constrains choices
        raise MalformedArgumentError(f"tg-export: unknown command {args.command!r}")
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
    except NotAuthorizedError as exc:
        # Stable, greppable token msgbrowse routes into its re-auth card.
        print(NOT_AUTHORIZED_TOKEN, file=sys.stderr)
        log_event("not_authorized", level=30, command=args.command, **exc.context)
        return exc.exit_code
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
