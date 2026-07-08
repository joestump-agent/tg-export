"""100% synthetic fixture data for the tg-export test harness.

Every name, number, id, and message body here is invented — no real account's
content appears anywhere (SPEC-0001 REQ "Testing"). These objects are the single
source of truth for the committed golden files: ``write_golden`` renders them with
the production canonical serializer (``tg_export.jsonio``), so the golden tree and
these dicts can never silently disagree.

The valid set exercises every branch of the contract: plain text, resolved links,
media (downloaded and skip-stub), a service event, reactions, a reply, a forward,
an unresolved sender, and a self chat. ``MALFORMED_MESSAGES`` holds entries that
MUST be rejected by the shipped JSON Schema.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from tg_export import jsonio

# Governing: SPEC-0001 REQ "Testing", REQ "JSON Output Contract"

# A fixed export timestamp keeps the golden manifest deterministic. Real runs vary
# this, but a fixture MUST be byte-stable, so it is pinned here.
GENERATED_AT = 1719900000

ACCOUNT: dict[str, Any] = {
    "id": 424242,
    "username": "trailmix",
    "phone_last4": "6789",
}

# --- Chat 1001: a supergroup exercising the full message surface -------------
CHAT_1001_MESSAGES: list[dict[str, Any]] = [
    {
        "id": 10,
        "chat_id": 1001,
        "date": 1719792000,
        "kind": "message",
        "from": {"name": "Ada Copeland", "is_self": False, "id": 900001, "username": "adacope"},
        "text": "Anyone up for the ridge loop on Saturday?",
    },
    {
        "id": 11,
        "chat_id": 1001,
        "date": 1719792600,
        "kind": "message",
        "from": {"name": "Ben Ortiz", "is_self": False, "id": 900002, "username": "benortiz"},
        "text": "Route notes are here, and the trailhead pin",
        "entities": [
            {"type": "url", "url": "https://example.org/ridge-loop"},
            {"type": "text_link", "url": "https://maps.example.org/pin/482"},
        ],
    },
    {
        "id": 12,
        "chat_id": 1001,
        "date": 1719793200,
        "kind": "message",
        "from": {"name": "Ada Copeland", "is_self": False, "id": 900001, "username": "adacope"},
        "text": "Trailhead this morning",
        "reactions": [
            {"emoji": "\U0001f44d", "count": 3},
            {"emoji": "\U0001f525", "count": 1},
        ],
        "media": {
            "kind": "photo",
            "mime": "image/jpeg",
            "size": 184320,
            "path": "media/1001/12.jpg",
            "width": 1280,
            "height": 960,
        },
    },
    {
        "id": 13,
        "chat_id": 1001,
        "date": 1719793800,
        "kind": "message",
        "from": {"name": "Ben Ortiz", "is_self": False, "id": 900002, "username": "benortiz"},
        "text": "I'm in!",
        "reply_to_message_id": 10,
        "reactions": [{"emoji": "❤", "count": 2}],
    },
    {
        "id": 14,
        "chat_id": 1001,
        "date": 1719794400,
        "kind": "message",
        "from": {"name": "Ada Copeland", "is_self": False, "id": 900001, "username": "adacope"},
        "text": "Forwarded forecast: clear skies through the weekend",
        "forward": {"from_name": "Mountain Weather Bot", "from_id": 700700, "date": 1719788000},
    },
    {
        "id": 15,
        "chat_id": 1001,
        "date": 1719795000,
        "kind": "message",
        "from": {"name": "Unknown", "is_self": False, "id": None},
        "text": "",
        "media": {
            "kind": "video",
            "mime": "video/mp4",
            "size": 73400320,
            "path": None,
            "skipped": True,
            "width": 1920,
            "height": 1080,
            "duration": 42.5,
            "filename": "clip.mp4",
        },
    },
    {
        "id": 16,
        "chat_id": 1001,
        "date": 1719795600,
        "kind": "service",
        "from": {"name": "Ben Ortiz", "is_self": False, "id": 900002, "username": "benortiz"},
        "text": "",
        "action": {"type": "chat_joined", "user_id": 900002},
    },
    {
        "id": 17,
        "chat_id": 1001,
        "date": 1719796200,
        "edit_date": 1719900500,
        "kind": "message",
        "from": {"name": "Ada Copeland", "is_self": False, "id": 900001, "username": "adacope"},
        "text": "Updated packing list attached",
        "media": {
            "kind": "document",
            "mime": "application/pdf",
            "size": 20480,
            "path": "media/1001/17.pdf",
            "filename": "packing.pdf",
        },
    },
]

# --- Chat 5005: the account's own Saved Messages (type "self") ---------------
CHAT_5005_MESSAGES: list[dict[str, Any]] = [
    {
        "id": 20,
        "chat_id": 5005,
        "date": 1719810000,
        "kind": "message",
        "from": {"name": "Trail Mix", "is_self": True, "id": 424242, "username": "trailmix"},
        "text": "Remember to charge the GPS the night before.",
    },
    {
        "id": 21,
        "chat_id": 5005,
        "date": 1719810600,
        "kind": "service",
        "from": {"name": "Trail Mix", "is_self": True, "id": 424242, "username": "trailmix"},
        "text": "",
        "action": {"type": "pin_message", "message_id": 20},
    },
]

# chat_id -> (chat manifest metadata without file/counts, ordered messages)
CHATS: dict[int, dict[str, Any]] = {
    1001: {
        "type": "supergroup",
        "title": "Weekend Hikers",
        "username": "weekendhikers",
        "messages": CHAT_1001_MESSAGES,
    },
    5005: {
        "type": "self",
        "title": "Saved Messages",
        "username": None,
        "messages": CHAT_5005_MESSAGES,
    },
}


def _chat_entry(chat_id: int, meta: dict[str, Any]) -> dict[str, Any]:
    messages = meta["messages"]
    dates = [m["date"] for m in messages]
    return {
        "id": chat_id,
        "type": meta["type"],
        "title": meta["title"],
        "username": meta["username"],
        "message_count": len(messages),
        "max_message_id": max(m["id"] for m in messages),
        "min_date": min(dates),
        "max_date": max(dates),
        "file": f"chats/{chat_id}.ndjson",
    }


def build_manifest() -> dict[str, Any]:
    """Build the synthetic manifest dict from the chat fixtures."""
    return {
        "schema_version": 1,
        "tool": "tg-export",
        "tool_version": "0.1.0",
        "generated_at": GENERATED_AT,
        "account": ACCOUNT,
        "chats": [_chat_entry(cid, meta) for cid, meta in CHATS.items()],
    }


def all_valid_messages() -> list[dict[str, Any]]:
    """Every valid synthetic message across all chats, in emission order."""
    out: list[dict[str, Any]] = []
    for meta in CHATS.values():
        out.extend(meta["messages"])
    return out


def write_golden(root: str | os.PathLike[str]) -> None:
    """Render the synthetic archive into ``root`` using the production serializer."""
    root = Path(root)
    jsonio.write_manifest(root / "manifest.json", build_manifest())
    for chat_id, meta in CHATS.items():
        jsonio.write_ndjson(root / "chats" / f"{chat_id}.ndjson", meta["messages"])


# --- Malformed entries: each MUST be rejected by the shipped JSON Schema ------
MALFORMED_MESSAGES: list[tuple[str, dict[str, Any]]] = [
    (
        "missing required kind",
        {
            "id": 90,
            "chat_id": 1001,
            "date": 1719800000,
            "from": {"name": "Ada Copeland", "is_self": False, "id": 900001},
            "text": "no kind field",
        },
    ),
    (
        "invalid kind enum",
        {
            "id": 91,
            "chat_id": 1001,
            "date": 1719800000,
            "kind": "bogus",
            "from": {"name": "Ada Copeland", "is_self": False, "id": 900001},
            "text": "bad kind",
        },
    ),
    (
        "entity leaks a utf-16 offset field",
        {
            "id": 92,
            "chat_id": 1001,
            "date": 1719800000,
            "kind": "message",
            "from": {"name": "Ada Copeland", "is_self": False, "id": 900001},
            "text": "offset leak",
            "entities": [{"type": "url", "url": "https://example.org/x", "offset": 5, "length": 3}],
        },
    ),
    (
        "service message missing action",
        {
            "id": 93,
            "chat_id": 1001,
            "date": 1719800000,
            "kind": "service",
            "from": {"name": "Ada Copeland", "is_self": False, "id": 900001},
            "text": "",
        },
    ),
    (
        "from missing is_self",
        {
            "id": 94,
            "chat_id": 1001,
            "date": 1719800000,
            "kind": "message",
            "from": {"name": "Ada Copeland", "id": 900001},
            "text": "no is_self",
        },
    ),
    (
        "unknown top-level field",
        {
            "id": 95,
            "chat_id": 1001,
            "date": 1719800000,
            "kind": "message",
            "from": {"name": "Ada Copeland", "is_self": False, "id": 900001},
            "text": "extra field",
            "downloaded_at": 1719900000,
        },
    ),
]
