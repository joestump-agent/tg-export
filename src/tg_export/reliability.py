"""Flood-wait survival helpers (M6 — Reliability and Rate Limits).

Telethon raises :class:`telethon.errors.FloodWaitError` when Telegram asks the
client to back off for ``err.seconds``. A flood-wait is NEVER fatal (SPEC-0001 REQ
"Reliability and Rate Limits"): every place it can arise — message iteration and
media download — catches it, sleeps the requested duration, logs the wait in
seconds at INFO (seconds only, no bodies), and resumes.

The sleep goes through the module-level :data:`_sleep` seam (defaulting to
:func:`asyncio.sleep`) so tests can inject a recorder and never actually sleep. It
is resolved through the module at call time, so monkeypatching ``reliability._sleep``
is honoured.

# Governing: SPEC-0001 REQ "Reliability and Rate Limits", REQ "Error Handling
#            Standards"; ADR-0002
"""

from __future__ import annotations

import asyncio
from typing import Any

from .logging import log_event

#: Injectable sleep seam. Tests monkeypatch ``reliability._sleep`` with an async
#: recorder so no real time passes; production uses ``asyncio.sleep``.
_sleep = asyncio.sleep


async def sleep_flood(exc: Any, **context: Any) -> int:
    """Log a Telethon ``FloodWaitError`` and sleep its requested seconds; resume.

    ``context`` carries only greppable, non-secret fields (chat/message ids); the
    logged record is ``event=flood_wait seconds=<n> ...`` — never a message body
    (SPEC-0001 REQ "Error Handling Standards"). Returns the seconds slept so a
    caller may accumulate them.
    """
    seconds = int(getattr(exc, "seconds", 0) or 0)
    log_event("flood_wait", seconds=seconds, **context)
    await _sleep(seconds)
    return seconds


__all__ = ["sleep_flood"]
