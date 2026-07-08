"""Shared test scaffolding: an offline network guard and a fake Telethon client.

The whole suite runs fully offline (SPEC-0001 REQ "Testing"): the ``no_network``
autouse fixture makes any attempt to open a socket connection a hard failure, so a
stray real network call cannot pass silently. ``FakeTelegramClient`` is the seam
M3 builds the real Telethon->contract mapping against — it mimics the async
surface tg-export uses (``iter_dialogs``/``iter_messages``/``get_me``) without a
network, replaying the synthetic fixtures.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import pytest

import synthetic

# Governing: SPEC-0001 REQ "Testing" (offline, mocked Telethon, synthetic fixtures)


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


@pytest.fixture
def golden_dir() -> Path:
    """Path to the committed golden export tree."""
    return Path(__file__).parent / "fixtures" / "golden"


class FakeDialog:
    """Minimal stand-in for a Telethon dialog."""

    def __init__(self, chat_id: int, meta: dict[str, Any]) -> None:
        self.id = chat_id
        self.name = meta["title"]
        self.title = meta["title"]
        self._type = meta["type"]
        self._messages = meta["messages"]


class FakeTelegramClient:
    """Offline fake of the async Telethon client surface tg-export consumes.

    M1 only asserts this never touches the network and can replay the fixtures;
    M3 wires the real mapping against this same seam.
    """

    def __init__(self, chats: dict[int, dict[str, Any]], account: dict[str, Any]) -> None:
        self._chats = chats
        self._account = account
        self.connected = False

    async def __aenter__(self) -> FakeTelegramClient:
        self.connected = True
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.connected = False

    async def get_me(self) -> dict[str, Any]:
        return dict(self._account)

    async def iter_dialogs(self):
        for chat_id, meta in self._chats.items():
            yield FakeDialog(chat_id, meta)

    async def iter_messages(self, chat_id: int, *, min_id: int = 0, **_: Any):
        for message in self._chats[chat_id]["messages"]:
            if message["id"] > min_id:
                yield message


@pytest.fixture
def fake_client() -> FakeTelegramClient:
    """A fake client preloaded with the synthetic fixtures."""
    return FakeTelegramClient(synthetic.CHATS, synthetic.ACCOUNT)


class FakeAuthClient:
    """Offline stand-in matching ``telethon.TelegramClient``'s auth surface.

    Constructed with Telethon's ``(session, api_id, api_hash, **kwargs)`` signature
    so it can be monkeypatched in for the real class. Behavior (already authorized?
    connect failure?) is configured per-test via :func:`telethon_stub`. It records
    every prompt callback it is handed so hygiene tests can prove the phone/code/
    2FA values never escape into logs. It never touches the network.
    """

    #: Class-level knobs set by the ``telethon_stub`` installer before construction.
    authorized: bool = True
    connect_error: BaseException | None = None
    instances: list[FakeAuthClient] = []

    def __init__(self, session: str, api_id: int, api_hash: str, **_: Any) -> None:
        self.session = session
        self.api_id = api_id
        self.api_hash = api_hash
        self._authorized = type(self).authorized
        self._connect_error = type(self).connect_error
        self.connected = False
        self.started = False
        type(self).instances.append(self)

    async def connect(self) -> None:
        if self._connect_error is not None:
            raise self._connect_error
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def is_user_authorized(self) -> bool:
        return self._authorized

    async def start(
        self,
        phone: Any = None,
        code_callback: Any = None,
        password: Any = None,
    ) -> FakeAuthClient:
        # Mimic Telethon driving the interactive dance by invoking each callback.
        if callable(phone):
            phone()
        if callable(code_callback):
            code_callback()
        if callable(password):
            password()
        self.started = True
        self._authorized = True
        self.connected = True
        return self

    async def get_me(self) -> dict[str, Any]:
        return {"id": 424242}

    def takeout(self, **kwargs: Any) -> FakeTakeout:
        # Mirrors client.takeout(...) returning an async context manager.
        self.takeout_kwargs = kwargs
        self.takeout_exc_type: type[BaseException] | None = None
        self.takeout_finalized_success: bool | None = None
        return FakeTakeout(self)


class FakeTakeout:
    """Async CM standing in for a Telethon takeout session.

    Records the ``exc_type`` its ``__aexit__`` receives so a test can prove a
    consumer-side error is forwarded (and thus the takeout is NOT finalized as a
    success). Never suppresses the exception.
    """

    def __init__(self, client: FakeAuthClient) -> None:
        self._client = client

    async def __aenter__(self) -> FakeAuthClient:
        self._client.takeout_entered = True
        return self._client

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self._client.takeout_exc_type = exc_type
        self._client.takeout_finalized_success = exc_type is None
        return False


@pytest.fixture
def telethon_stub(monkeypatch: pytest.MonkeyPatch):
    """Install a configurable offline stand-in for ``telethon.TelegramClient``.

    Returns an installer: ``telethon_stub(authorized=..., connect_error=...)`` that
    patches ``telethon.TelegramClient`` and returns the stub class (whose
    ``instances`` list captures every client the code under test built).
    """

    def install(
        *, authorized: bool = True, connect_error: BaseException | None = None
    ) -> type[FakeAuthClient]:
        FakeAuthClient.authorized = authorized
        FakeAuthClient.connect_error = connect_error
        FakeAuthClient.instances = []
        monkeypatch.setattr("telethon.TelegramClient", FakeAuthClient)
        return FakeAuthClient

    return install
