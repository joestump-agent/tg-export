"""100% synthetic fixture data + the offline Telethon fake for the test harness.

Every name, number, id, and message body here is invented — no real account's
content appears anywhere (SPEC-0001 REQ "Testing"). The fixtures are Telethon-
*shaped* objects (they mirror the TL class names the production mapping dispatches
on), so :func:`tg_export.mapping.map_message` and the whole ``tg_export.export``
walk run against them exactly as they would against a live client — only offline.

This module is the single source of truth for the committed golden tree:
:func:`write_golden` runs the *real* export pipeline over these fakes with a pinned
``generated_at``, so the golden files and these objects can never silently disagree
(a drift shows up as a golden-file test failure).

The valid set exercises every branch of the contract: plain text, resolved links
(including a channel post whose URL sits right after a non-BMP emoji, to prove the
UTF-16 offset slicing is correct — ADR-0005), media (photo/video/document), a
service event, reactions, a reply, a forward, an unresolved sender, a self chat,
and a broadcast channel (excluded from the default scope; ADR-0007).
``MALFORMED_MESSAGES`` holds contract dicts that MUST be rejected by the shipped
JSON Schema.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tg_export import export, mapping

# Governing: SPEC-0001 REQ "Testing", REQ "JSON Output Contract",
#            "Message Mapping Fidelity"; ADR-0005, ADR-0007

# A fixed export timestamp keeps the golden manifest deterministic. Real runs vary
# this, but a fixture MUST be byte-stable, so it is pinned here.
GENERATED_AT = 1719900000

ACCOUNT: dict[str, Any] = {
    "id": 424242,
    "username": "trailmix",
    "phone_last4": "6789",
}
SELF_ID: int = ACCOUNT["id"]


def _dt(unix_seconds: int) -> datetime:
    """A tz-aware UTC datetime, exactly like Telethon hands the mapper."""
    return datetime.fromtimestamp(unix_seconds, tz=UTC)


# --- Telethon-shaped fake objects (mirror the TL class names) ----------------


class User:
    def __init__(
        self,
        id: int,
        first_name: str | None = None,
        last_name: str | None = None,
        username: str | None = None,
    ) -> None:
        self.id = id
        self.first_name = first_name
        self.last_name = last_name
        self.username = username


class Channel:
    def __init__(
        self,
        id: int,
        title: str,
        username: str | None = None,
        *,
        broadcast: bool = False,
        megagroup: bool = False,
    ) -> None:
        self.id = id
        self.title = title
        self.username = username
        self.broadcast = broadcast
        self.megagroup = megagroup


class MessageEntityUrl:
    def __init__(self, offset: int, length: int) -> None:
        self.offset = offset
        self.length = length


class MessageEntityTextUrl:
    def __init__(self, offset: int, length: int, url: str) -> None:
        self.offset = offset
        self.length = length
        self.url = url


class PeerUser:
    """Telethon Peer wrapping a user id (what a real ``fwd_from.from_id`` carries)."""

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id


class PeerChannel:
    def __init__(self, channel_id: int) -> None:
        self.channel_id = channel_id


class ReactionEmoji:
    def __init__(self, emoticon: str) -> None:
        self.emoticon = emoticon


class ReactionCustomEmoji:
    """A custom (uploaded) reaction — carries a document id, not an emoticon."""

    def __init__(self, document_id: int) -> None:
        self.document_id = document_id


class ReactionCount:
    def __init__(self, reaction: Any, count: int) -> None:
        self.reaction = reaction
        self.count = count


class MessageReactions:
    def __init__(self, results: list[ReactionCount]) -> None:
        self.results = results


class MessageFwdHeader:
    def __init__(self, from_name: str, from_id: Any, date: datetime | None) -> None:
        self.from_name = from_name
        # Real Telethon hands a Peer here (PeerUser/PeerChannel), not a bare int.
        self.from_id = from_id
        self.date = date


class PhotoSize:
    def __init__(self, w: int, h: int, size: int) -> None:
        self.w = w
        self.h = h
        self.size = size


class Photo:
    def __init__(self, sizes: list[PhotoSize]) -> None:
        self.sizes = sizes


class MessageMediaPhoto:
    def __init__(self, photo: Photo) -> None:
        self.photo = photo


class DocumentAttributeVideo:
    def __init__(self, w: int, h: int, duration: float, round_message: bool = False) -> None:
        self.w = w
        self.h = h
        self.duration = duration
        self.round_message = round_message


class DocumentAttributeFilename:
    def __init__(self, file_name: str) -> None:
        self.file_name = file_name


class Document:
    def __init__(self, mime_type: str, size: int, attributes: list[Any]) -> None:
        self.mime_type = mime_type
        self.size = size
        self.attributes = attributes


class MessageMediaDocument:
    def __init__(self, document: Document) -> None:
        self.document = document


class MessageActionChatJoinedByLink:
    def __init__(self, user_id: int | None = None) -> None:
        self.user_id = user_id


class MessageActionPinMessage:
    def __init__(self, message_id: int | None = None) -> None:
        self.message_id = message_id


class Msg:
    """A Telethon-shaped message; unset fields default to the "absent" value."""

    def __init__(
        self,
        *,
        id: int,
        date: datetime,
        message: str = "",
        out: bool = False,
        sender: Any = None,
        reply_to_msg_id: int | None = None,
        fwd_from: MessageFwdHeader | None = None,
        reactions: MessageReactions | None = None,
        entities: list[Any] | None = None,
        media: Any = None,
        action: Any = None,
        edit_date: datetime | None = None,
    ) -> None:
        self.id = id
        self.date = date
        self.message = message
        self.out = out
        self.sender = sender
        self.reply_to_msg_id = reply_to_msg_id
        self.fwd_from = fwd_from
        self.reactions = reactions
        self.entities = entities
        self.media = media
        self.action = action
        self.edit_date = edit_date


def _url_entity(text: str, substr: str) -> MessageEntityUrl:
    """A ``url`` entity spanning ``substr`` in ``text`` (UTF-16 offsets, like TG)."""
    idx = text.index(substr)
    offset = len(text[:idx].encode("utf-16-le")) // 2
    length = len(substr.encode("utf-16-le")) // 2
    return MessageEntityUrl(offset, length)


def _text_link(text: str, substr: str, url: str) -> MessageEntityTextUrl:
    idx = text.index(substr)
    offset = len(text[:idx].encode("utf-16-le")) // 2
    length = len(substr.encode("utf-16-le")) // 2
    return MessageEntityTextUrl(offset, length, url)


# --- senders -----------------------------------------------------------------

ADA = User(900001, "Ada", "Copeland", username="adacope")
BEN = User(900002, "Ben", "Ortiz", username="benortiz")
SELF_USER = User(SELF_ID, "Trail", "Mix", username="trailmix")
ALERTS_CHANNEL = Channel(2002, "Trail Alerts", username="trailalerts", broadcast=True)

# --- Chat 1001: a supergroup exercising the full message surface -------------
_MSG11_TEXT = "Route notes: https://example.org/ridge-loop — trailhead pin"

CHAT_1001_MESSAGES: list[Msg] = [
    Msg(
        id=10,
        date=_dt(1719792000),
        message="Anyone up for the ridge loop on Saturday?",
        sender=ADA,
    ),
    Msg(
        id=11,
        date=_dt(1719792600),
        message=_MSG11_TEXT,
        sender=BEN,
        entities=[
            _url_entity(_MSG11_TEXT, "https://example.org/ridge-loop"),
            _text_link(_MSG11_TEXT, "trailhead pin", "https://maps.example.org/pin/482"),
        ],
    ),
    Msg(
        id=12,
        date=_dt(1719793200),
        message="Trailhead this morning",
        sender=ADA,
        reactions=MessageReactions(
            [
                ReactionCount(ReactionEmoji("\U0001f44d"), 3),
                ReactionCount(ReactionEmoji("\U0001f525"), 1),
            ]
        ),
        # Size kept small so the committed golden's downloaded blob stays tiny; the
        # oversize (--max-media-mb) path is exercised with its own fixture in tests.
        media=MessageMediaPhoto(Photo([PhotoSize(1280, 960, 3072)])),
    ),
    Msg(
        id=13,
        date=_dt(1719793800),
        message="I'm in!",
        sender=BEN,
        reply_to_msg_id=10,
        reactions=MessageReactions([ReactionCount(ReactionEmoji("❤"), 2)]),
    ),
    Msg(
        id=14,
        date=_dt(1719794400),
        message="Forwarded forecast: clear skies through the weekend",
        sender=ADA,
        # from_id is a Peer (as real Telethon supplies) -> unwrapped to int 700700.
        fwd_from=MessageFwdHeader("Mountain Weather Bot", PeerUser(700700), _dt(1719788000)),
    ),
    Msg(
        id=15,
        date=_dt(1719795000),
        message="",
        sender=None,  # unresolved sender -> "Unknown"
        media=MessageMediaDocument(
            Document(
                "video/mp4",
                4096,
                [DocumentAttributeVideo(1920, 1080, 42.5), DocumentAttributeFilename("clip.mp4")],
            )
        ),
    ),
    Msg(
        id=16,
        date=_dt(1719795600),
        message="",
        sender=BEN,
        action=MessageActionChatJoinedByLink(user_id=900002),
    ),
    Msg(
        id=17,
        date=_dt(1719796200),
        edit_date=_dt(1719900500),
        message="Updated packing list attached",
        sender=ADA,
        media=MessageMediaDocument(
            Document("application/pdf", 4096, [DocumentAttributeFilename("packing.pdf")])
        ),
    ),
]

# --- Chat 5005: the account's own Saved Messages (type "self") ---------------
CHAT_5005_MESSAGES: list[Msg] = [
    Msg(
        id=20,
        date=_dt(1719810000),
        message="Remember to charge the GPS the night before.",
        sender=SELF_USER,
        out=True,
    ),
    Msg(
        id=21,
        date=_dt(1719810600),
        message="",
        sender=SELF_USER,
        out=True,
        action=MessageActionPinMessage(message_id=20),
    ),
]

# --- Chat 2002: a broadcast channel (excluded from the default scope) --------
# The first post places a URL immediately after a non-BMP emoji so a naive
# character-offset slice would corrupt the URL — proving the UTF-16 slicing.
_MSG30_TEXT = "\U0001f6a7 Trailhead road closed: https://example.org/alerts/17"

CHAT_2002_MESSAGES: list[Msg] = [
    Msg(
        id=30,
        date=_dt(1719820000),
        message=_MSG30_TEXT,
        sender=ALERTS_CHANNEL,  # channel post -> from.id null
        entities=[_url_entity(_MSG30_TEXT, "https://example.org/alerts/17")],
        # A custom (uploaded) reaction: no emoticon, only a document id -> str id.
        reactions=MessageReactions([ReactionCount(ReactionCustomEmoji(5555001), 4)]),
    ),
    Msg(
        id=31,
        date=_dt(1719820600),
        message="Aid station open at the summit.",
        sender=ALERTS_CHANNEL,
    ),
]

# chat_id -> dialog metadata + ordered (chronological) raw messages. Insertion
# order is the dialog-walk order; the channel sits between the two default chats
# to prove it is skipped without disturbing the 1001/5005 golden ordering.
RAW_CHATS: dict[int, dict[str, Any]] = {
    1001: {
        "type": "supergroup",
        "title": "Weekend Hikers",
        "username": "weekendhikers",
        "entity_id": 1001,
        "megagroup": True,
        "broadcast": False,
        "is_user": False,
        "messages": CHAT_1001_MESSAGES,
    },
    2002: {
        "type": "channel",
        "title": "Trail Alerts",
        "username": "trailalerts",
        "entity_id": 2002,
        "megagroup": False,
        "broadcast": True,
        "is_user": False,
        "messages": CHAT_2002_MESSAGES,
    },
    5005: {
        "type": "self",
        "title": "Saved Messages",
        "username": None,
        # Saved Messages resolves to the account's own user entity.
        "entity_id": SELF_ID,
        "megagroup": False,
        "broadcast": False,
        "is_user": True,
        "messages": CHAT_5005_MESSAGES,
    },
}


# --- the offline Telethon fake -----------------------------------------------

#: A fixed, inspectable fill pattern. Deterministic synthetic media bytes keep the
#: committed golden media blobs byte-stable across runs (ADR-0004).
_MEDIA_FILL = b"tg-export synthetic media payload\n"


def _synthetic_media_bytes(size: int) -> bytes:
    """Deterministic bytes of exactly ``size`` length (what the fake "downloads")."""
    if size <= 0:
        return b""
    reps = size // len(_MEDIA_FILL) + 1
    return (_MEDIA_FILL * reps)[:size]


class FakeEntity:
    """The ``dialog.entity`` a Telethon dialog exposes (User or Channel-shaped)."""

    def __init__(self, meta: dict[str, Any]) -> None:
        self.id = meta["entity_id"]
        self.username = meta["username"]
        self.title = meta["title"]
        self.megagroup = meta["megagroup"]
        self.broadcast = meta["broadcast"]


class FakeDialog:
    """A Telethon-shaped dialog the export walk classifies and titles."""

    def __init__(self, chat_id: int, meta: dict[str, Any]) -> None:
        self.id = chat_id
        self.name = meta["title"]
        self.title = meta["title"]
        self.entity = FakeEntity(meta)
        self.is_user = meta["is_user"]
        self.is_group = meta["type"] == "group"
        self.is_channel = meta["type"] in ("supergroup", "channel")


class FakeTelegramClient:
    """Offline fake of the async Telethon client surface the export consumes.

    Replays the synthetic fixtures without a network. ``iter_messages`` yields in
    stored (chronological) order regardless of ``reverse`` — the fixtures are
    already oldest-first, which is what ``export`` requests via ``reverse=True`` —
    and honours ``min_id`` so the M5 ``--since`` seam can be exercised offline.
    """

    def __init__(
        self,
        chats: dict[int, dict[str, Any]] | None = None,
        account: dict[str, Any] | None = None,
    ) -> None:
        self._chats = chats if chats is not None else RAW_CHATS
        self._account = account if account is not None else ACCOUNT
        self.connected = False
        #: Count of real download_media invocations, so idempotency (existing file
        #: not re-fetched) can be asserted by call count.
        self.download_calls = 0

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
            if message.id > min_id:
                yield message

    async def download_media(self, message: Any, file: str | None = None) -> str | None:
        """Offline stand-in for Telethon's ``download_media``.

        Writes a deterministic file of exactly the media's declared ``size`` bytes to
        ``file`` (so the idempotent exact-size check is faithful), records the call,
        and never touches the network.
        """
        self.download_calls += 1
        if file is None:
            return None
        meta = mapping.map_media(getattr(message, "media", None))
        size = int(meta["size"]) if meta is not None else 0
        target = Path(file)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_synthetic_media_bytes(size))
        return str(target)


# --- mapped-output helpers (single source of truth for goldens) --------------


def _mapped(chat_id: int) -> list[dict[str, Any]]:
    meta = RAW_CHATS[chat_id]
    return [
        mapping.map_message(msg, chat_id=chat_id, self_id=SELF_ID) for msg in meta["messages"]
    ]


def default_scope_ids() -> list[int]:
    """Chat ids that a default (non-channel) export includes, in walk order."""
    return [cid for cid, meta in RAW_CHATS.items() if meta["type"] != "channel"]


def build_manifest() -> dict[str, Any]:
    """The default-scope manifest, assembled through the production helpers."""
    entries = [
        export.chat_manifest_entry(
            cid, RAW_CHATS[cid]["type"], RAW_CHATS[cid]["title"], RAW_CHATS[cid]["username"],
            _mapped(cid),
        )
        for cid in default_scope_ids()
    ]
    return export.build_manifest(
        export.account_block(ACCOUNT), entries, generated_at=GENERATED_AT
    )


def all_valid_messages() -> list[dict[str, Any]]:
    """Every valid synthetic message across all chats (mapped), in emission order."""
    out: list[dict[str, Any]] = []
    for chat_id in RAW_CHATS:
        out.extend(_mapped(chat_id))
    return out


def write_golden(root: str | os.PathLike[str]) -> None:
    """Render the synthetic archive into ``root`` via the real export pipeline.

    Runs a default-scope export (channels excluded) with the pinned
    ``generated_at`` so the produced tree is the canonical golden.
    """
    client = FakeTelegramClient()
    config = export.ExportConfig(output=Path(root), chats=None, generated_at=GENERATED_AT)
    asyncio.run(export.export_with_client(client, config))


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
