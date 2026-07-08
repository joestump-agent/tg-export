"""Sentinel exceptions and the single source-of-truth exit-code contract.

msgbrowse invokes tg-export non-interactively and must classify failures from the
exit code alone — especially "re-auth needed" (an expired/unauthorized session)
versus a network problem versus a usage mistake. This module defines the domain
sentinel errors and maps each to a *distinct, stable* exit code. The mapping lives
here and nowhere else; the CLI imports these codes rather than re-deriving them.

The not-authorized path additionally carries a stable stderr token
(``tg-export: not authorized``) that msgbrowse greps for to raise its re-auth card.

# Governing: SPEC-0001 REQ "CLI Surface", REQ "Error Handling Standards"; ADR-0009
"""

from __future__ import annotations

# --- Exit-code table (stable contract; changing a value is a breaking change) ---
EXIT_OK = 0
#: Generic, otherwise-unclassified runtime failure.
EXIT_RUNTIME = 1
#: Malformed argument / usage error. Matches argparse's own exit(2) so the two
#: sources of "bad invocation" collapse onto one code.
EXIT_MALFORMED_ARG = 2
#: Session missing, expired, or otherwise unauthorized — msgbrowse's re-auth card.
EXIT_NOT_AUTHORIZED = 3
#: Transport/network failure talking to Telegram.
EXIT_NETWORK = 4

#: Stable, greppable stderr token for the unauthorized/expired-session case.
NOT_AUTHORIZED_TOKEN = "tg-export: not authorized"


class TgExportError(Exception):
    """Base for every tg-export domain error.

    Each subclass declares the exit code the CLI must return when it surfaces.
    ``context`` holds structured key/value pairs safe to log (never secrets).
    """

    exit_code: int = EXIT_RUNTIME

    def __init__(self, message: str, **context: object) -> None:
        super().__init__(message)
        self.context: dict[str, object] = context


class MalformedArgumentError(TgExportError):
    """A missing/invalid argument or credential — a usage mistake by the caller."""

    exit_code = EXIT_MALFORMED_ARG


class NotAuthorizedError(TgExportError):
    """The session is missing, expired, or not authorized for the account."""

    exit_code = EXIT_NOT_AUTHORIZED


class NetworkError(TgExportError):
    """A transport-level failure reaching Telegram's data centers."""

    exit_code = EXIT_NETWORK


class ExportError(TgExportError):
    """A failure during the dialog walk / mapping / write pipeline.

    Raised with layer-boundary context (``chat <id>: message <id>: <cause>``) when
    a message fails to map or an emitted object fails the shipped-schema reject
    gate. Maps to the generic runtime exit code — it is neither an auth nor a
    network nor a usage mistake, but a genuine processing failure the caller must
    see rather than have silently swallowed (SPEC-0001 REQ "Error Handling
    Standards").
    """

    exit_code = EXIT_RUNTIME


def exit_code_for(exc: BaseException) -> int:
    """Return the CLI exit code for ``exc`` (the single mapping used everywhere)."""
    if isinstance(exc, TgExportError):
        return exc.exit_code
    return EXIT_RUNTIME
