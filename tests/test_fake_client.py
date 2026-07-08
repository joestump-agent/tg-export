"""The offline test seam M3 will build the real Telethon mapping against.

M1 asserts the fake client replays the synthetic fixtures without a network and
that the ``no_network`` guard is actually armed (SPEC-0001 REQ "Testing")."""

from __future__ import annotations

import asyncio
import socket

import pytest


def test_network_guard_blocks_outbound_connections():
    with pytest.raises(RuntimeError):
        socket.create_connection(("example.org", 80), timeout=0.01)


def test_fake_client_replays_dialogs_and_messages(fake_client):
    async def run():
        async with fake_client as client:
            me = await client.get_me()
            dialog_ids = [d.id async for d in client.iter_dialogs()]
            first_chat = dialog_ids[0]
            msgs = [m async for m in client.iter_messages(first_chat)]
            return me, dialog_ids, msgs

    me, dialog_ids, msgs = asyncio.run(run())
    assert me["id"] == 424242
    # Telethon-shaped objects now flow through the seam (attribute, not subscript).
    assert dialog_ids == [1001, 2002, 5005]
    assert [m.id for m in msgs] == [10, 11, 12, 13, 14, 15, 16, 17]


def test_fake_client_min_id_anchor_yields_only_newer(fake_client):
    # Proves the seam M5 uses for incremental --since anchoring works offline.
    async def run():
        async with fake_client as client:
            return [m.id async for m in client.iter_messages(1001, min_id=14)]

    assert asyncio.run(run()) == [15, 16, 17]
