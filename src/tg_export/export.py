"""The dialog walk, per-chat NDJSON writer, and manifest builder.

This is the M3 workhorse (SPEC-0001 REQ "CLI Surface", "JSON Output Contract"):

1. resolve the account (``get_me``) and its self id;
2. enumerate dialogs, applying scope — the DEFAULT run exports private, group,
   supergroup, and self dialogs and EXCLUDES channels; ``--chats ID,ID,...`` is the
   opt-in that restricts the run to exactly the listed ids, of any type, so it
   doubles as the channel opt-in (ADR-0007);
3. for each in-scope chat, iterate messages in chronological order, map each with
   :func:`tg_export.mapping.map_message`, validate it against the shipped schema
   (the reject gate — an invalid object fails the run loudly, never silently), and
   append it to ``chats/<chat_id>.ndjson`` as it is produced (ADR-0003);
4. write the complete, validated ``manifest.json`` last.

Errors are wrapped with layer-boundary context (``chat <id>: message <id>:
<cause>``) and never swallowed; progress is reported through structured
:func:`log_event` records that carry only counts, ids, and types — never a message
body (SPEC-0001 REQ "Error Handling Standards", "Security and Secret Hygiene").

Seams left clean for later milestones:
* **M4 media download** lives behind :func:`tg_export.mapping.map_media`; the walk
  here already emits the ``media`` metadata block with ``path: null``.

M5 incremental (``--since``/``--full``) is now live: a ``--since`` run reads the
prior manifest's per-chat ``max_message_id`` anchors, threads each as
``iter_messages(min_id=...)``, and appends only-new messages in place; ``--full``
ignores anchors and re-exports everything (SPEC-0001 REQ "Incremental Export";
ADR-0008).

# Governing: SPEC-0001 REQ "CLI Surface", "JSON Output Contract", "Error Handling
#            Standards"; ADR-0002, ADR-0003, ADR-0007
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import ValidationError

from . import __version__, auth, jsonio, mapping, schemas
from .errors import ExportError, MalformedArgumentError
from .logging import log_event, phone_last4

# Governing: ADR-0007 — the default sweep excludes channels.
DEFAULT_SCOPE_TYPES = frozenset({"private", "group", "supergroup", "self"})


@dataclass
class ExportConfig:
    """Everything the walk needs, resolved from the CLI surface.

    ``chats`` is ``None`` for the default (non-channel) scope, or a frozenset of ids
    for the ``--chats`` opt-in. ``no_media`` / ``max_media_mb`` are the M4 media
    knobs (accepted and threaded now, inert until M4). ``since`` / ``full`` are the
    M5 incremental knobs. ``generated_at`` is a test/reproducibility seam: when set
    it pins the manifest timestamp (golden fixtures need a stable value); a real run
    leaves it ``None`` and stamps wall-clock time.
    """

    output: Path
    chats: frozenset[int] | None = None
    no_media: bool = False
    max_media_mb: int | None = None
    since: Path | None = None  # M5
    full: bool = False  # M5
    generated_at: int | None = None


# --- account / dialog classification -----------------------------------------


def account_block(me: Any) -> dict[str, Any]:
    """Build the manifest ``account`` block from ``get_me`` (User or dict).

    ``phone_last4`` carries at most the last four digits; the full number never
    enters the manifest (SPEC-0001 REQ "Security and Secret Hygiene").
    """
    if isinstance(me, dict):
        account_id = int(me["id"])
        username = me.get("username")
        last4 = me.get("phone_last4")
        if last4 is None and me.get("phone"):
            last4 = phone_last4(me["phone"])
    else:
        account_id = int(me.id)
        username = getattr(me, "username", None)
        last4 = phone_last4(getattr(me, "phone", None))
    return {"id": account_id, "username": username, "phone_last4": last4}


def classify_dialog(dialog: Any, self_id: int | None) -> str:
    """Classify a Telethon dialog into a contract chat ``type``.

    ``self`` is the account's own Saved Messages (a user dialog whose entity is the
    account itself); other user dialogs are ``private``. A channel entity is a
    ``supergroup`` when it is a megagroup, else a broadcast ``channel``. Anything
    else is a legacy basic ``group``.
    """
    entity = getattr(dialog, "entity", None)
    if getattr(dialog, "is_user", False):
        entity_id = getattr(entity, "id", None)
        if entity_id is not None and entity_id == self_id:
            return "self"
        return "private"
    if getattr(dialog, "is_channel", False):
        return "supergroup" if getattr(entity, "megagroup", False) else "channel"
    return "group"


def _dialog_username(dialog: Any) -> str | None:
    entity = getattr(dialog, "entity", None)
    return getattr(entity, "username", None)


def _dialog_title(dialog: Any) -> str:
    return getattr(dialog, "title", None) or getattr(dialog, "name", None) or str(dialog.id)


def in_scope(chat_id: int, chat_type: str, chats_filter: frozenset[int] | None) -> bool:
    """Apply the ADR-0007 scope rule.

    With ``--chats`` given, only the listed ids export (any type, so channels opt
    in). Without it, everything but a broadcast ``channel`` exports.
    """
    if chats_filter is not None:
        return chat_id in chats_filter
    return chat_type != "channel"


# --- manifest assembly -------------------------------------------------------


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
        # The incremental --since anchor (ADR-0008); 0 for an empty chat.
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


# --- incremental (--since / --full) anchors ----------------------------------
# Governing: SPEC-0001 REQ "Incremental Export"; ADR-0008 (append-in-place)


def _resolve_anchors(config: ExportConfig) -> dict[int, int]:
    """Return the per-chat ``max_message_id`` anchors that govern this run.

    ``--full`` (or a config with no ``--since``) yields ``{}`` — no anchors, so
    every chat is re-exported from scratch (the M3 truncate behavior). Otherwise the
    prior directory's manifest is read and each chat's recorded ``max_message_id`` is
    returned; a chat present here is resumed via ``min_id`` and appended in place,
    while a chat absent from it (a new chat) exports in full (ADR-0008). ``--full``
    wins over ``--since`` when both are given.
    """
    if config.full or config.since is None:
        return {}
    return _prior_anchors(Path(config.since))


def _prior_anchors(since_dir: Path) -> dict[int, int]:
    """Read ``<since_dir>/manifest.json`` and map ``chat id -> max_message_id``."""
    manifest_path = since_dir / "manifest.json"
    try:
        manifest = jsonio.read_manifest(manifest_path)
    except (OSError, ValueError) as exc:
        # A caller-supplied --since dir without a readable manifest is a usage
        # mistake, surfaced as the stable malformed-argument exit code.
        raise MalformedArgumentError(
            f"tg-export: --since {since_dir}: cannot read manifest.json: {exc}"
        ) from exc
    try:
        # A prior manifest that parses as JSON but is structurally wrong (not the
        # expected shape, or a chat entry lacking max_message_id) is still a usage
        # mistake, not a generic runtime crash — keep it on the greppable arg code.
        return {int(chat["id"]): int(chat["max_message_id"]) for chat in manifest.get("chats", [])}
    except (KeyError, TypeError, AttributeError) as exc:
        raise MalformedArgumentError(
            f"tg-export: --since {since_dir}: manifest chat entry missing max_message_id"
        ) from exc


def _read_chat_messages(output: Path, chat_id: int) -> list[dict[str, Any]]:
    """Read a chat's on-disk NDJSON back into objects (for a complete manifest).

    On an append-in-place ``--since`` run the fresh manifest must count the WHOLE
    file (prior lines + newly appended lines), so the manifest entry is built from
    what is on disk, not just this run's new messages.
    """
    path = output / "chats" / f"{chat_id}.ndjson"
    if not path.exists():
        return []
    objects: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line:
            continue
        try:
            objects.append(json.loads(line))
        except json.JSONDecodeError as exc:
            # A pre-existing half-written/corrupt prior line (e.g. after a SIGKILL
            # mid-write) must surface with chat/line context rather than a raw decode
            # error. M6 will upgrade this to best-effort skip-and-continue; for now it
            # fails loudly but greppably (SPEC-0001 REQ "Error Handling Standards").
            raise ExportError(
                f"chat {chat_id}: prior NDJSON line {lineno} is malformed: {exc}",
                chat=chat_id,
            ) from exc
    return objects


# --- the walk ----------------------------------------------------------------


async def _export_chat(
    client: Any,
    *,
    chat_id: int,
    chat_type: str,
    self_id: int | None,
    output: Path,
    min_id: int = 0,
    append: bool = False,
) -> list[dict[str, Any]]:
    """Iterate, map, validate, and append one chat's messages; return the mapped list.

    ``min_id`` is the incremental lower bound threaded into ``iter_messages`` so a
    ``--since`` run fetches only messages with id greater than the prior anchor
    (ADR-0008). ``append`` opens the writer in ``"a"`` mode so those newer messages
    are appended past the chat's existing lines rather than truncating them; a full
    or new-chat run leaves ``append=False`` (the M3 truncate behavior).

    A message that fails to map or fails schema validation raises
    :class:`ExportError` with ``chat <id>: message <id>: <cause>`` context — the
    reject gate. (Best-effort per-message tolerance is M6; M3 fails loudly.)
    """
    mapped: list[dict[str, Any]] = []
    ndjson_path = output / "chats" / f"{chat_id}.ndjson"
    # Deliberate: opening the writer creates chats/<id>.ndjson eagerly, so an
    # in-scope chat with zero messages leaves a valid 0-byte file plus a manifest
    # entry (message_count 0). This keeps the manifest index and the on-disk file
    # set in lockstep and is what --since (M5) reopens to append to.
    # reverse=True => chronological (oldest-first) deterministic order (ADR-0003).
    # min_id anchors the --since incremental lower bound (M5, ADR-0008).
    with jsonio.ndjson_writer(ndjson_path, mode="a" if append else "w") as write_line:
        async for raw in client.iter_messages(chat_id, reverse=True, min_id=min_id):
            raw_id = getattr(raw, "id", "?")
            try:
                obj = mapping.map_message(raw, chat_id=chat_id, self_id=self_id)
            except Exception as exc:  # noqa: BLE001 - re-raised with boundary context
                raise ExportError(
                    f"chat {chat_id}: message {raw_id}: mapping failed: {exc}",
                    chat=chat_id,
                    msg=raw_id,
                ) from exc
            try:
                schemas.validate("message", obj)
            except ValidationError as exc:
                raise ExportError(
                    f"chat {chat_id}: message {raw_id}: schema validation failed: {exc.message}",
                    chat=chat_id,
                    msg=raw_id,
                ) from exc
            write_line(obj)
            mapped.append(obj)
    return mapped


async def export_with_client(client: Any, config: ExportConfig) -> dict[str, Any]:
    """Run the full export against an already-open, authorized client.

    This is the seam tests and golden generation drive directly with the offline
    fake client; :func:`run_export` is the production wrapper that opens a real
    (takeout) client first. Returns the written manifest dict.
    """
    me = await client.get_me()
    account = account_block(me)
    self_id = account["id"]
    output = Path(config.output)
    generated_at = config.generated_at if config.generated_at is not None else int(time.time())

    # Governing: SPEC-0001 REQ "Incremental Export"; ADR-0008. Anchors are empty for
    # a full/default run; on --since they carry each prior chat's max_message_id.
    anchors = _resolve_anchors(config)

    chat_entries: list[dict[str, Any]] = []
    async for dialog in client.iter_dialogs():
        chat_id = int(dialog.id)
        chat_type = classify_dialog(dialog, self_id)
        if not in_scope(chat_id, chat_type, config.chats):
            log_event("chat_skipped", chat=chat_id, type=chat_type)
            continue
        # A chat present in the prior manifest resumes from its anchor and appends
        # in place; a chat absent from it (new chat) or a --full run exports fresh.
        anchor = anchors.get(chat_id)
        append = anchor is not None
        min_id = anchor if append else 0
        new_messages = await _export_chat(
            client,
            chat_id=chat_id,
            chat_type=chat_type,
            self_id=self_id,
            output=output,
            min_id=min_id,
            append=append,
        )
        # The fresh manifest must reflect the COMPLETE file (prior + appended), so
        # an append run rereads the on-disk NDJSON; a fresh run's new_messages ARE
        # the whole file.
        file_messages = _read_chat_messages(output, chat_id) if append else new_messages
        chat_entries.append(
            chat_manifest_entry(
                chat_id, chat_type, _dialog_title(dialog), _dialog_username(dialog), file_messages
            )
        )
        log_event(
            "chat_exported",
            chat=chat_id,
            type=chat_type,
            messages=len(new_messages),
            total=len(file_messages),
        )

    manifest = build_manifest(account, chat_entries, generated_at=generated_at)
    # Reject gate: the manifest must validate before it is written.
    schemas.validate("manifest", manifest)
    jsonio.write_manifest(output / "manifest.json", manifest)
    log_event(
        "export_complete",
        chats=len(chat_entries),
        messages=sum(entry["message_count"] for entry in chat_entries),
    )
    return manifest


async def list_chats_with_client(client: Any) -> list[dict[str, Any]]:
    """Discovery listing (used by ``tg-export chats``) — ALL dialogs, channels too.

    ``chats`` lists everything so a user can find channel ids to opt into ``--chats``
    (ADR-0007). It is a cheap listing: no message walk, ids/types/titles only.
    """
    me = await client.get_me()
    self_id = account_block(me)["id"]
    listing: list[dict[str, Any]] = []
    async for dialog in client.iter_dialogs():
        listing.append(
            {
                "id": int(dialog.id),
                "type": classify_dialog(dialog, self_id),
                "title": _dialog_title(dialog),
                "username": _dialog_username(dialog),
            }
        )
    return listing


# --- production wrappers (open a real client via the auth seam) --------------


def run_export(
    config: ExportConfig,
    *,
    session: str | Path,
    credential: auth.ApiCredential,
) -> dict[str, Any]:
    """Open an authorized takeout client and run the export; return the manifest."""

    async def _run() -> dict[str, Any]:
        # takeout=True: more forgiving flood limits for bulk history (ADR-0002).
        async with auth.open_client(
            session=session, credential=credential, takeout=True
        ) as client:
            return await export_with_client(client, config)

    return asyncio.run(_run())


def list_chats(
    *,
    session: str | Path,
    credential: auth.ApiCredential,
) -> list[dict[str, Any]]:
    """Open an authorized client and return the discovery chat listing."""

    async def _run() -> list[dict[str, Any]]:
        async with auth.open_client(session=session, credential=credential) as client:
            return await list_chats_with_client(client)

    return asyncio.run(_run())


__all__ = [
    "DEFAULT_SCOPE_TYPES",
    "ExportConfig",
    "account_block",
    "build_manifest",
    "chat_manifest_entry",
    "classify_dialog",
    "export_with_client",
    "in_scope",
    "list_chats",
    "list_chats_with_client",
    "run_export",
]
