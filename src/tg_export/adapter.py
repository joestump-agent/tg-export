"""tdl-raw JSON  ->  Telethon-shaped objects that :mod:`tg_export.mapping` consumes.

This is the single seam that couples tg-export to tdl's output. Everything else in
the tool is protocol-agnostic; the whole "which exporter produced the bytes"
question lives here and nowhere else (ADR-0011).

Why an adapter exists at all
----------------------------
``mapping.map_message`` is a *pure, offline* function that reads a Telethon-shaped
message via duck-typed ``getattr`` + ``type(x).__name__`` dispatch. tdl captures
the SAME underlying MTProto ``message`` objects, but serializes them through
**gotd (Go)**, whose JSON key/type names differ from Telethon's Python classes.
So the data is all there; the field names are not the ones the mapper reads. This
module reshapes gotd-flavoured JSON into the Telethon-shaped attribute surface the
mapper already handles, and then the entire existing fidelity path (senders,
service events, reactions, replies, forwards, link-entity resolution, media
metadata) runs unchanged.

===========================================================================
VERIFICATION GATES — this adapter is a SKELETON until these are confirmed
against a real `tdl chat export -c <chat> --all --raw` dump. See ADR-0011.
===========================================================================

1. COMPLETENESS. Confirm tdl's ``--raw`` emits the full MTProto ``Message`` /
   ``MessageService`` object per message, so ``reply_to``, ``fwd_from``,
   ``reactions``, ``action``, ``entities`` and ``media`` are all present. Wire
   each into the corresponding Telethon-shaped attribute below.

2. ENTITY MAPS (the sharp one). A raw message carries ``from_id`` as a bare peer
   id, NOT a display name — names live in a separate ``users[]`` / ``chats[]``
   array on the enclosing response. Confirm whether tdl's dump preserves that
   map. If it does, :func:`adapt_message` resolves ``from_id -> name`` through
   ``entity_index`` and emits a full sender. If it does NOT, the sender degrades
   to ``id``-only (``name: "Unknown"``) — still a valid, stably-keyed contact for
   msgbrowse (ADR-0003 keys contacts by ``(source, identifier)``), just nameless
   until reconciled.

Until the gates are closed, this adapter maps the fields we are confident about
(id, date, text, reply-to, edit-date) and RESOLVES SENDERS ONLY when the source
already carries resolvable sender fields; the rest are left absent, which the
mapper renders as the documented degraded forms. The exact tdl field names are
marked ``TODO(tdl-shape)`` at each site so closing a gate is a local edit here.

# Governing: ADR-0001 (delegated exporter — the coupling is the input contract),
#            ADR-0005 (link-entity resolution), ADR-0011 (transform pivot);
#            SPEC-0001 REQ "Message Mapping Fidelity"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --- Telethon-shaped shim objects --------------------------------------------
# These mirror the attribute surface tg_export.mapping reads. Named to match the
# Telethon TL classes the mapper dispatches on via ``type(x).__name__`` (the same
# convention the synthetic test fixtures use), so map_message treats them exactly
# as it would a live client's objects.


class User:
    """Telethon ``User``-shaped sender (``_is_user_entity`` dispatches on the name)."""

    def __init__(self, id: int | None, *, first_name: str = "", last_name: str = "",
                 username: str | None = None) -> None:
        self.id = id
        self.first_name = first_name
        self.last_name = last_name
        self.username = username


@dataclass
class AdaptedMessage:
    """A Telethon-``Message``-shaped object built from one tdl-raw message.

    Only the attributes the mapper reads are modelled. Absent optional attributes
    default to ``None``/``False``, which is exactly what ``getattr(msg, ..., None)``
    in the mapper expects, so an under-populated message maps to its documented
    degraded form rather than crashing.
    """

    id: int
    date: int  # Unix seconds; mapping._epoch passes an int straight through
    message: str = ""
    out: bool = False
    sender: Any = None
    reply_to_msg_id: int | None = None
    edit_date: int | None = None
    # Fidelity fields wired as the verification gates close (TODO(tdl-shape)):
    entities: list[Any] = field(default_factory=list)
    reactions: Any = None
    fwd_from: Any = None
    action: Any = None
    media: Any = None


# --- tdl export container -----------------------------------------------------


@dataclass
class TdlChat:
    """One chat's worth of tdl-raw messages plus the manifest index metadata."""

    id: int
    type: str
    title: str
    username: str | None
    raw_messages: list[dict[str, Any]]


@dataclass
class TdlExport:
    """The parsed tdl export: the account, its chats, and a shared entity index."""

    account: dict[str, Any]
    self_id: int | None
    chats: list[TdlChat]
    #: peer id -> {"name": str, "username": str | None} resolved from the dump's
    #: users[]/chats[] arrays (VERIFICATION GATE 2). Empty until that map is wired.
    entity_index: dict[int, dict[str, Any]] = field(default_factory=dict)


# --- adaptation ---------------------------------------------------------------


def _resolve_sender(from_id: int | None, out: bool, entity_index: dict[int, dict[str, Any]]):
    """Build a Telethon-shaped sender from a peer id via the entity index.

    Returns ``None`` (the mapper's "Unknown"/id-null degrade) when the id is absent
    or unresolved in the entity index — the documented behaviour when the tdl dump
    lacks the ``users[]`` map (VERIFICATION GATE 2).
    """
    if from_id is None:
        return None
    entry = entity_index.get(int(from_id))
    if entry is None:
        # id known but name unresolved: still emit an id-only sender so msgbrowse
        # can key a stable (nameless) contact on it.
        return User(int(from_id))
    return User(
        int(from_id),
        first_name=entry.get("name", "") or "",
        username=entry.get("username"),
    )


def adapt_message(raw: dict[str, Any], *, entity_index: dict[int, dict[str, Any]] | None = None):
    """Reshape one tdl-raw message dict into an :class:`AdaptedMessage`.

    Confident scalar fields are mapped now; the fidelity fields (entities,
    reactions, forwards, service actions, media) are left for the verification
    gates and marked ``TODO(tdl-shape)`` so wiring each is a local edit.
    """
    index = entity_index or {}
    # TODO(tdl-shape): confirm the exact key names below against a real --raw dump.
    # gotd serializes MTProto field names in its own casing; these are the expected
    # semantic fields, not yet the confirmed literal keys.
    msg_id = int(raw["id"])
    date = int(raw["date"])
    out = bool(raw.get("out", False))
    from_id = raw.get("from_id")
    if isinstance(from_id, dict):
        # A raw Peer wrapper, e.g. {"user_id": N}; unwrap to the bare id.
        from_id = from_id.get("user_id") or from_id.get("channel_id") or from_id.get("chat_id")

    return AdaptedMessage(
        id=msg_id,
        date=date,
        message=raw.get("text") or raw.get("message") or "",
        out=out,
        sender=_resolve_sender(from_id, out, index),
        reply_to_msg_id=raw.get("reply_to_message_id"),
        edit_date=raw.get("edit_date"),
        # TODO(tdl-shape) GATE 1: wire raw["entities"] -> Telethon MessageEntity*
        # shims, raw["reactions"] -> MessageReactions shim, raw["fwd_from"] ->
        # MessageFwdHeader shim, raw["action"] -> MessageAction* shim, and
        # raw["media"]/raw["file"] -> MessageMediaPhoto/Document shim + path.
    )


__all__ = [
    "AdaptedMessage",
    "TdlChat",
    "TdlExport",
    "User",
    "adapt_message",
]
