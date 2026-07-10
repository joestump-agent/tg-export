"""Shared test scaffolding: an offline network guard and logging reset.

The whole suite runs fully offline (SPEC-0001 REQ "Testing"): the ``no_network``
autouse fixture makes any attempt to open a socket connection a hard failure, so a
stray real network call cannot pass silently. Since the transform pivot (ADR-0011)
the tool has no client of its own to fake — it maps a tdl dump offline — so the
synthetic fixtures are plain Telethon-*shaped* objects the mapper consumes directly.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest

from tg_export import logging as tg_logging

# Governing: SPEC-0001 REQ "Testing" (offline, synthetic fixtures); ADR-0011


@pytest.fixture(autouse=True)
def reset_logging() -> Any:
    """Reset tg-export logging state after each test.

    The ``--json-logs`` toggle and the installed stderr handler are process-global;
    resetting between tests keeps the shared-process suite isolated so one test's
    ``configure(json_logs=True)`` cannot leak into another's assertions.
    """
    yield
    tg_logging.reset()


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything attempts an outbound socket connection."""

    def _blocked(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError(
            "network access is forbidden in tests (SPEC-0001 REQ \"Testing\")"
        )

    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
