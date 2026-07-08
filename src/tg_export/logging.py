"""Structured, key-value logging for tg-export.

Error and progress reporting MUST be structured (key=value pairs, not string
interpolation) and MUST NEVER carry message bodies or secret material — logs hold
counts, ids, and paths only (SPEC-0001 REQ "Error Handling Standards", REQ
"Security and Secret Hygiene"). Every log line therefore goes through
:func:`log_event`, which renders a fixed ``event=<name> k=v ...`` shape. Callers
pass only non-secret fields; there is no formatter that could interpolate a body.

# Governing: SPEC-0001 REQ "Error Handling Standards", REQ "Security and Secret Hygiene"
"""

from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger("tg_export")


def _render(event: str, fields: dict[str, Any]) -> str:
    parts = [f"event={event}"]
    parts.extend(f"{key}={value}" for key, value in fields.items())
    return " ".join(parts)


def log_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    """Emit one structured log record: ``event=<event> key=value ...``.

    Only non-secret, greppable fields (counts, ids, paths) may be passed. There is
    deliberately no way to log a raw message body or a secret through this helper.
    """
    _LOGGER.log(level, _render(event, fields))


def phone_last4(phone: str | None) -> str | None:
    """Reduce a phone number to at most its last 4 digits (never the full number).

    Used only if a phone hint must ever be surfaced; the full number is discarded.
    """
    if not phone:
        return None
    digits = [c for c in phone if c.isdigit()]
    return "".join(digits[-4:]) if digits else None
