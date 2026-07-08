"""Pure Telethon-message -> contract-dict mapping.

This module is the single place that turns a Telethon ``Message`` object into one
NDJSON contract object (SPEC-0001 REQ "Message Mapping Fidelity"). It is a pure
function of its input: no network, no clock, no absolute paths, no run-varying
fields — so re-mapping an equal message always yields an equal dict, which is what
keeps the export byte-stable (ADR-0004).

What it preserves, and how:

* **Sender** (``from``) — resolved display name, ``is_self``, id, and username.
  An unresolved sender degrades to ``name: "Unknown"`` with ``id: null`` (the
  message is still emitted). A channel/anonymous-admin sender emits ``id: null``.
* **Service events** — a message carrying a Telethon ``action`` becomes
  ``kind: "service"`` plus an ``action`` object; content messages carry no
  ``action`` key at all (the schema forbids a non-null action on a ``message``).
* **Link entities** — ``MessageEntityUrl`` / ``MessageEntityTextUrl`` are resolved
  to their absolute ``url`` and emitted as ``{type, url}``. UTF-16 offsets NEVER
  cross the boundary (ADR-0005); the ``url``-entity URL is recovered by slicing the
  text on the entity's UTF-16 offsets *inside* this module.
* **Reactions / replies / forwards / edits** — mapped to ``reactions[]``,
  ``reply_to_message_id``, ``forward``, and ``edit_date``.
* **Media** — metadata only in M3: ``kind``/``mime``/``size`` plus cheap
  ``width``/``height``/``duration``/``filename``, with ``path: null``. The actual
  download that fills ``path`` (and the oversize ``skipped`` stub) is M4; it plugs
  into :func:`map_media` / the export media seam without touching non-media lines.

The duck-typed reads (``getattr`` + ``type(x).__name__`` dispatch) work identically
against real Telethon TL objects and the synthetic Telethon-shaped fakes the test
harness feeds, since the fakes mirror the TL class names.

# Governing: SPEC-0001 REQ "Message Mapping Fidelity"; ADR-0005 (resolve link
#            entities, no UTF-16 offsets), ADR-0003 (contract object)
"""

from __future__ import annotations

import re
from typing import Any

# --- primitive helpers -------------------------------------------------------


def _epoch(value: Any) -> int | None:
    """Return Unix seconds (UTC) for a Telethon datetime (or pass an int through)."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    # Telethon dates are tz-aware UTC datetimes; .timestamp() is the epoch.
    return int(value.timestamp())


def _utf16_slice(text: str, offset: int, length: int) -> str:
    """Slice ``text`` by UTF-16 code-unit ``offset``/``length`` (Telegram's model).

    Telegram entity indices are UTF-16 code units. Slicing on the UTF-16 encoding
    here recovers the exact substring a ``url`` entity points at without ever
    letting an offset escape into the emitted object (ADR-0005).
    """
    encoded = text.encode("utf-16-le")
    return encoded[offset * 2 : (offset + length) * 2].decode("utf-16-le")


def _as_int_id(value: Any) -> int | None:
    """Coerce a Telethon Peer (or bare int) to an int id, else ``None``."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    for attr in ("user_id", "channel_id", "chat_id"):
        found = getattr(value, attr, None)
        if found is not None:
            return int(found)
    return None


# --- sender ------------------------------------------------------------------


def _is_user_entity(entity: Any) -> bool:
    # Telethon User (and the synthetic fake) is the only "person" sender; a Chat or
    # Channel sender is an anonymous admin / channel post and must emit id: null.
    return type(entity).__name__ == "User"


def _display_name(entity: Any) -> str:
    if _is_user_entity(entity):
        first = getattr(entity, "first_name", None) or ""
        last = getattr(entity, "last_name", None) or ""
        name = " ".join(part for part in (first, last) if part).strip()
        if name:
            return name
        username = getattr(entity, "username", None)
        return username or "Unknown"
    # Chat / Channel: the title is the display name.
    return getattr(entity, "title", None) or "Unknown"


def _map_from(msg: Any, self_id: int | None) -> dict[str, Any]:
    """Build the ``from`` block; unresolved sender degrades to ``Unknown``/null."""
    out = bool(getattr(msg, "out", False))
    sender = getattr(msg, "sender", None)
    if sender is None:
        # Unresolved sender (deleted account / not prefetched) — still emit.
        return {"name": "Unknown", "is_self": out, "id": None}
    name = _display_name(sender)
    if not _is_user_entity(sender):
        # Anonymous admin / channel post: no personal id crosses the boundary.
        return {"name": name, "is_self": out, "id": None}
    sender_id = getattr(sender, "id", None)
    is_self = out or (sender_id is not None and sender_id == self_id)
    block: dict[str, Any] = {"name": name, "is_self": is_self, "id": sender_id}
    username = getattr(sender, "username", None)
    if username:
        block["username"] = username
    return block


# --- entities ----------------------------------------------------------------


def _map_entities(msg: Any) -> list[dict[str, str]]:
    text = getattr(msg, "message", None) or ""
    entities = getattr(msg, "entities", None) or []
    resolved: list[dict[str, str]] = []
    for entity in entities:
        name = type(entity).__name__
        if name == "MessageEntityUrl":
            # The URL is the text the entity spans (UTF-16 offsets, resolved here).
            url = _utf16_slice(text, entity.offset, entity.length)
            resolved.append({"type": "url", "url": url})
        elif name == "MessageEntityTextUrl":
            # A hidden link carries its target explicitly.
            resolved.append({"type": "text_link", "url": getattr(entity, "url", "") or ""})
        # Other entity types (bold, mention, ...) are intentionally dropped (ADR-0005).
    return resolved


# --- reactions ---------------------------------------------------------------


def _map_reactions(msg: Any) -> list[dict[str, Any]]:
    reactions = getattr(msg, "reactions", None)
    if reactions is None:
        return []
    out: list[dict[str, Any]] = []
    for result in getattr(reactions, "results", None) or []:
        reaction = getattr(result, "reaction", None)
        emoji = getattr(reaction, "emoticon", None)
        if emoji is None:
            # Custom emoji: carry the document id as a string (schema: emoji string).
            doc_id = getattr(reaction, "document_id", None)
            emoji = str(doc_id) if doc_id is not None else ""
        out.append({"emoji": emoji, "count": int(getattr(result, "count", 0) or 0)})
    return out


# --- forward -----------------------------------------------------------------


def _map_forward(msg: Any) -> dict[str, Any] | None:
    fwd = getattr(msg, "fwd_from", None)
    if fwd is None:
        return None
    from_name = getattr(fwd, "from_name", None) or "Unknown"
    date = getattr(fwd, "date", None)
    return {
        "from_name": from_name,
        "from_id": _as_int_id(getattr(fwd, "from_id", None)),
        "date": _epoch(date),
    }


# --- media (metadata only in M3; M4 fills path/skipped) ----------------------

_MEDIA_DEFAULT_MIME = "application/octet-stream"


def _largest_photo_size(photo: Any) -> Any | None:
    best: Any | None = None
    for size in getattr(photo, "sizes", None) or []:
        width = getattr(size, "w", None)
        if width is None:
            continue
        if best is None or width > (getattr(best, "w", 0) or 0):
            best = size
    return best


def _map_photo(media: Any) -> dict[str, Any]:
    photo = getattr(media, "photo", None)
    width = height = None
    size_bytes: int | None = None
    if photo is not None:
        best = _largest_photo_size(photo)
        if best is not None:
            width = getattr(best, "w", None)
            height = getattr(best, "h", None)
            size_bytes = getattr(best, "size", None)
            if size_bytes is None:
                # Progressive photo sizes carry cumulative byte counts.
                progressive = getattr(best, "sizes", None)
                if progressive:
                    size_bytes = progressive[-1]
    # Telegram photos are JPEG.
    out: dict[str, Any] = {
        "kind": "photo",
        "mime": "image/jpeg",
        "size": int(size_bytes) if size_bytes is not None else 0,
        "path": None,
    }
    if width is not None:
        out["width"] = int(width)
    if height is not None:
        out["height"] = int(height)
    return out


def _map_document(media: Any) -> dict[str, Any]:
    doc = getattr(media, "document", None)
    mime = getattr(doc, "mime_type", None) or _MEDIA_DEFAULT_MIME
    size_bytes = int(getattr(doc, "size", 0) or 0)

    width = height = None
    duration: float | None = None
    filename: str | None = None
    has_video = has_audio = is_sticker = is_animated = is_voice = is_round = False

    for attr in getattr(doc, "attributes", None) or []:
        name = type(attr).__name__
        if name == "DocumentAttributeFilename":
            filename = getattr(attr, "file_name", None)
        elif name == "DocumentAttributeVideo":
            has_video = True
            width = getattr(attr, "w", None)
            height = getattr(attr, "h", None)
            duration = getattr(attr, "duration", None)
            is_round = bool(getattr(attr, "round_message", False))
        elif name == "DocumentAttributeAudio":
            has_audio = True
            duration = getattr(attr, "duration", None)
            is_voice = bool(getattr(attr, "voice", False))
        elif name == "DocumentAttributeSticker":
            is_sticker = True
        elif name == "DocumentAttributeAnimated":
            is_animated = True
        elif name == "DocumentAttributeImageSize":
            width = getattr(attr, "w", None)
            height = getattr(attr, "h", None)

    # Precedence: sticker/animation classify the document before generic video/audio.
    if is_sticker:
        kind = "sticker"
    elif is_animated:
        kind = "animation"
    elif has_video:
        kind = "video_note" if is_round else "video"
    elif has_audio:
        kind = "voice" if is_voice else "audio"
    else:
        kind = "document"

    out: dict[str, Any] = {"kind": kind, "mime": mime, "size": size_bytes, "path": None}
    if width is not None:
        out["width"] = int(width)
    if height is not None:
        out["height"] = int(height)
    if duration is not None:
        out["duration"] = float(duration)
    if filename is not None:
        out["filename"] = filename
    return out


def map_media(media: Any) -> dict[str, Any] | None:
    """Map a Telethon media object to the ``media`` metadata block, or ``None``.

    Returns ``None`` for non-file media (webpage/poll/geo/contact...), which carry
    no downloadable payload. In M3 the ``path`` is always ``null`` — this is the
    seam M4 plugs the download into: M4 sets ``path`` (and, for oversize files, the
    ``skipped`` stub) here without disturbing any non-media line.
    """
    name = type(media).__name__
    if name == "MessageMediaPhoto":
        return _map_photo(media)
    if name == "MessageMediaDocument":
        return _map_document(media)
    return None


# --- service actions ---------------------------------------------------------

#: Telethon action class -> stable contract ``type`` string. Anything absent falls
#: back to a snake_case of the class name (minus the ``MessageAction`` prefix).
_ACTION_TYPES: dict[str, str] = {
    "MessageActionPinMessage": "pin_message",
    "MessageActionChatJoinedByLink": "chat_joined",
    "MessageActionChatAddUser": "chat_add_user",
    "MessageActionChatDeleteUser": "chat_delete_user",
    "MessageActionChatEditTitle": "chat_edit_title",
    "MessageActionChatEditPhoto": "chat_edit_photo",
    "MessageActionChatDeletePhoto": "chat_delete_photo",
    "MessageActionChatCreate": "chat_create",
    "MessageActionChannelCreate": "channel_create",
    "MessageActionPhoneCall": "phone_call",
    "MessageActionContactSignUp": "contact_sign_up",
}

#: Cheap, JSON-safe scalar fields copied onto an action when present. Kept to a
#: whitelist so a rich TL sub-object never leaks into the (JSON) output.
_ACTION_SCALAR_FIELDS: tuple[str, ...] = (
    "user_id",
    "message_id",
    "inviter_id",
    "duration",
    "title",
    "call_id",
)

_CAMEL_RE = re.compile(r"(?<!^)(?=[A-Z])")


def _snake(camel: str) -> str:
    return _CAMEL_RE.sub("_", camel).lower()


def _map_action(action: Any) -> dict[str, Any]:
    name = type(action).__name__
    if name in _ACTION_TYPES:
        type_str = _ACTION_TYPES[name]
    elif name.startswith("MessageAction"):
        type_str = _snake(name[len("MessageAction") :]) or "unknown"
    else:
        type_str = _snake(name)
    out: dict[str, Any] = {"type": type_str}
    for field in _ACTION_SCALAR_FIELDS:
        if not hasattr(action, field):
            continue
        value = getattr(action, field)
        if isinstance(value, (int, float, str)):
            out[field] = value
        elif isinstance(value, list) and all(isinstance(item, int) for item in value):
            out[field] = value
    # 'users' (a list of ids) is common on join/add actions.
    users = getattr(action, "users", None)
    if isinstance(users, list) and all(isinstance(item, int) for item in users):
        out["users"] = users
    return out


# --- top-level map -----------------------------------------------------------


def map_message(msg: Any, *, chat_id: int, self_id: int | None) -> dict[str, Any]:
    """Map one Telethon message to its NDJSON contract dict (pure, deterministic).

    ``chat_id`` and ``self_id`` are threaded in because a Telethon message does not
    always self-identify its owning chat, and ``is_self`` needs the account id.
    """
    action = getattr(msg, "action", None)
    kind = "service" if action is not None else "message"

    date = _epoch(getattr(msg, "date", None))
    if date is None:
        # A real Telegram message always carries a date. A missing one is a genuine
        # anomaly, so raise loudly (the export reject gate wraps this with chat/msg
        # context) rather than silently papering it over with 0 — which would also
        # collapse a legitimate epoch-0 date. Consistent with the reject-gate policy.
        raise ValueError("message has no date")

    obj: dict[str, Any] = {
        "id": int(msg.id),
        "chat_id": int(chat_id),
        "date": date,
        "kind": kind,
        "from": _map_from(msg, self_id),
        # Visible text; media captions live in msg.message too, so this flattens both.
        "text": getattr(msg, "message", None) or "",
    }

    edit_date = getattr(msg, "edit_date", None)
    if edit_date is not None:
        obj["edit_date"] = _epoch(edit_date)

    entities = _map_entities(msg)
    if entities:
        obj["entities"] = entities

    reply_to = getattr(msg, "reply_to_msg_id", None)
    if reply_to is not None:
        obj["reply_to_message_id"] = int(reply_to)

    forward = _map_forward(msg)
    if forward is not None:
        obj["forward"] = forward

    reactions = _map_reactions(msg)
    if reactions:
        obj["reactions"] = reactions

    if kind == "service":
        obj["action"] = _map_action(action)

    media = getattr(msg, "media", None)
    if media is not None:
        mapped_media = map_media(media)
        if mapped_media is not None:
            obj["media"] = mapped_media

    return obj


__all__ = ["map_message", "map_media"]
