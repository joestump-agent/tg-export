"""Contract-level archive assembly: manifest blocks and per-chat index entries.

This module is deliberately protocol-agnostic. It knows the msgbrowse JSON output
contract (``schema_version: 1``) — the ``account`` block, each ``chats[]`` index
entry, and the top-level ``manifest`` — and nothing about where the messages came
from. It was salvaged from the retired live ``export`` module (ADR-0011): the walk,
the Telethon client, and the session/credential surface are gone, but the writer
and manifest assembly are the same contract they always were and are reused
unchanged by the tdl-raw transform (:mod:`tg_export.transform`).

The NDJSON writer itself lives in :mod:`tg_export.jsonio`; this module only builds
the dicts that get validated and written.

# Governing: ADR-0003 (directory/NDJSON contract), ADR-0004 (schema_version
#            lockstep, byte-stable output), ADR-0011 (transform pivot);
#            SPEC-0001 REQ "JSON Output Contract"
"""

from __future__ import annotations

from typing import Any

from . import __version__


def account_block(account: dict[str, Any]) -> dict[str, Any]:
    """Build the manifest ``account`` block from the tdl export's account info.

    Transform mode has no Telegram session and therefore no phone number to redact
    (that was the retired live path's concern, ADR-0009). ``phone_last4`` is carried
    through only if the source export already provides it, else ``None`` — the full
    number never appears here regardless (SPEC-0001 REQ "Security and Secret
    Hygiene").
    """
    return {
        "id": int(account["id"]),
        "username": account.get("username"),
        "phone_last4": account.get("phone_last4"),
    }


def chat_manifest_entry(
    chat_id: int,
    chat_type: str,
    title: str,
    username: str | None,
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build one ``manifest.chats[]`` entry from a chat's mapped messages."""
    dates = [m["date"] for m in messages]
    return {
        "id": chat_id,
        "type": chat_type,
        "title": title,
        "username": username,
        "message_count": len(messages),
        # The per-chat high-water id. msgbrowse content-hashes each message for
        # idempotent import, so incrementality is tdl's job upstream and msgbrowse's
        # downstream; this anchor is retained as a contract field (ADR-0008 lineage).
        "max_message_id": max((m["id"] for m in messages), default=0),
        "min_date": min(dates) if dates else None,
        "max_date": max(dates) if dates else None,
        "file": f"chats/{chat_id}.ndjson",
    }


def build_manifest(
    account: dict[str, Any],
    chat_entries: list[dict[str, Any]],
    *,
    generated_at: int,
    tool_version: str = __version__,
) -> dict[str, Any]:
    """Assemble the complete manifest dict (validated by the caller before write)."""
    return {
        "schema_version": 1,
        "tool": "tg-export",
        "tool_version": tool_version,
        "generated_at": generated_at,
        "account": account,
        "chats": chat_entries,
    }


__all__ = ["account_block", "build_manifest", "chat_manifest_entry"]
