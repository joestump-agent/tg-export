"""Structured, key-value logging for tg-export.

Error and progress reporting MUST be structured (key=value pairs, not string
interpolation) and MUST NEVER carry message bodies or secret material — logs hold
counts, ids, and paths only (SPEC-0001 REQ "Error Handling Standards", REQ
"Security and Secret Hygiene"). Every log line therefore goes through
:func:`log_event`, which renders a fixed ``event=<name> k=v ...`` shape. Callers
pass only non-secret fields; there is no formatter that could interpolate a body.

Output is structured either way. :func:`configure` switches the rendering between
the default ``event=<name> k=v ...`` text and a machine-ingestible one-JSON-object-
per-line form (``--json-logs``, M6). Neither form can carry a message body — both
render only the non-secret fields the caller passed.

# Governing: SPEC-0001 REQ "Error Handling Standards", REQ "Security and Secret
#            Hygiene", REQ "Reliability and Rate Limits"
"""

from __future__ import annotations

import json as _json
import logging
import sys
from typing import IO, Any

_LOGGER = logging.getLogger("tg_export")

#: When True, :func:`log_event` renders one JSON object per line instead of the
#: default ``event=<name> k=v`` text. Toggled by :func:`configure`.
_JSON_LOGS = False


def _render(event: str, fields: dict[str, Any]) -> str:
    if _JSON_LOGS:
        # One JSON object per line, machine-ingestible. ``default=str`` keeps a
        # stray non-JSON scalar (e.g. a Path) serializable; still no body ever
        # reaches here — only the non-secret fields the caller passed.
        return _json.dumps(
            {"event": event, **fields}, sort_keys=True, ensure_ascii=False, default=str
        )
    parts = [f"event={event}"]
    parts.extend(f"{key}={value}" for key, value in fields.items())
    return " ".join(parts)


def log_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Emit one structured log record: ``event=<event> key=value ...``.

    Only non-secret, greppable fields (counts, ids, paths) may be passed. There is
    deliberately no way to log a raw message body or a secret through this helper.
    The rendered shape (key=value text vs. one JSON object per line) is governed by
    :func:`configure`.
    """
    _LOGGER.log(level, _render(event, fields))


def configure(*, json_logs: bool = False, stream: IO[str] | None = None) -> None:
    """Install the tg-export log handler and select the output format.

    ``json_logs`` switches :func:`log_event` to one-JSON-object-per-line output
    (machine-ingestible) instead of the default key=value text (SPEC-0001 REQ
    "Reliability and Rate Limits"). A single stderr ``StreamHandler`` is installed
    at INFO so progress and errors are actually emitted; the handler is replaced
    (never duplicated) on repeat calls. Propagation is left intact so pytest's
    ``caplog`` still captures records.
    """
    global _JSON_LOGS
    _JSON_LOGS = json_logs
    target = stream if stream is not None else sys.stderr
    _remove_handlers()
    handler = logging.StreamHandler(target)
    # log_event already renders the full structured line; emit it verbatim.
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.set_name("tg_export")
    _LOGGER.addHandler(handler)
    _LOGGER.setLevel(logging.INFO)


def _remove_handlers() -> None:
    for handler in [h for h in _LOGGER.handlers if h.get_name() == "tg_export"]:
        _LOGGER.removeHandler(handler)


def reset() -> None:
    """Reset logging state to defaults (key=value text, no installed handler).

    Used by the test harness between tests so the ``--json-logs`` toggle and the
    installed handler never leak across the shared-process test suite.
    """
    global _JSON_LOGS
    _JSON_LOGS = False
    _remove_handlers()
    # Restore the logger to its default delegate-to-parent level so a prior
    # configure()'s INFO threshold never leaks into another test.
    _LOGGER.setLevel(logging.NOTSET)


def phone_last4(phone: str | None) -> str | None:
    """Reduce a phone number to at most its last 4 digits (never the full number).

    Used only if a phone hint must ever be surfaced; the full number is discarded.
    """
    if not phone:
        return None
    digits = [c for c in phone if c.isdigit()]
    return "".join(digits[-4:]) if digits else None
