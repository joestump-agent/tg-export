"""Sentinel exceptions and the single source-of-truth exit-code contract.

msgbrowse invokes tg-export non-interactively and classifies failures from the exit
code alone. Transform mode (ADR-0011) has no session or network of its own — auth is
tdl's job against an installed Telegram Desktop client — so the auth/network sentinel
classes of the retired live path are gone. What remains is a small, stable contract:
success, a usage mistake, a malformed tdl input, and a generic processing failure.

# Governing: SPEC-0001 REQ "CLI Surface", REQ "Error Handling Standards"; ADR-0011
"""

from __future__ import annotations

# --- Exit-code table (stable contract; changing a value is a breaking change) ---
EXIT_OK = 0
#: Generic, otherwise-unclassified runtime failure.
EXIT_RUNTIME = 1
#: Malformed argument / usage error. Matches argparse's own exit(2) so the two
#: sources of "bad invocation" collapse onto one code.
EXIT_MALFORMED_ARG = 2
#: The tdl export input is missing, unreadable, or structurally malformed — a
#: distinct code so msgbrowse can tell "bad/absent input" from a usage mistake.
EXIT_MALFORMED_INPUT = 5


class TgExportError(Exception):
    """Base for every tg-export domain error.

    Each subclass declares the exit code the CLI must return when it surfaces.
    ``context`` holds structured key/value pairs safe to log (never secrets).
    """

    exit_code: int = EXIT_RUNTIME

    def __init__(self, message: str, **context: object) -> None:
        super().__init__(message)
        self.context: dict[str, object] = context


class MalformedInputError(TgExportError):
    """The tdl export is missing, unreadable, or not the expected shape."""

    exit_code = EXIT_MALFORMED_INPUT


class TransformError(TgExportError):
    """A failure while reshaping/mapping/validating a message in the transform.

    Raised with layer-boundary context (``chat <id>: message <id>: <cause>``) when a
    message fails to adapt, map, or pass the shipped-schema reject gate. Maps to the
    generic runtime code — a genuine processing failure the caller must see rather
    than have silently swallowed (SPEC-0001 REQ "Error Handling Standards").
    """

    exit_code = EXIT_RUNTIME
