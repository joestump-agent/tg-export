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

Milestones now live:
* **M4 media download** (:mod:`tg_export.media`) interleaves with the walk: each
  message's media is downloaded and its ``media.path`` set in place before the
  NDJSON line is written.
* **M5 incremental** (``--since``/``--full``): a ``--since`` run reads the prior
  manifest's per-chat ``max_message_id`` anchors, threads each as
  ``iter_messages(min_id=...)``, and appends only-new messages in place; ``--full``
  ignores anchors and re-exports everything (SPEC-0001 REQ "Incremental Export";
  ADR-0008).
* **M6 reliability** (SPEC-0001 REQ "Reliability and Rate Limits"): a
  ``FloodWaitError`` during message iteration is slept and iteration resumes from
  the last written id (media flood-waits are handled inside :mod:`tg_export.media`);
  a single message that fails to map, download, or validate is logged and SKIPPED
  (best-effort tolerance — it no longer aborts the chat), and a single chat that
  fails on access is logged and skipped without aborting the run; and a killed run
  (which lacks the last-written manifest) is resumed by recomputing the per-chat
  anchors from the partial NDJSON so ``--since`` continues with no dupes/gaps.

# Governing: SPEC-0001 REQ "CLI Surface", "JSON Output Contract", "Error Handling
#            Standards", "Reliability and Rate Limits"; ADR-0002, ADR-0003,
#            ADR-0007, ADR-0008
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telethon.errors import FloodWaitError

from . import __version__, auth, jsonio, mapping, media, reliability, schemas
from .errors import (
    ExportError,
    MalformedArgumentError,
    NetworkError,
    NotAuthorizedError,
)
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
    """Map ``chat id -> max_message_id`` for a ``--since`` run.

    Precedence:

    * a present ``<since_dir>/manifest.json`` is authoritative — its per-chat
      ``max_message_id`` anchors govern the resume (the M5 behavior);
    * an ABSENT manifest but a populated ``chats/`` is the killed-run case (M6): the
      manifest is written last, so its absence means the prior run died mid-flight.
      Anchors are recomputed from the partial NDJSON (max id actually on disk) so
      the resume continues with no dupes/gaps (SPEC-0001 REQ "Reliability");
    * neither present is a genuine usage mistake — surfaced as the stable
      malformed-argument exit code.
    """
    manifest_path = since_dir / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = jsonio.read_manifest(manifest_path)
        except (OSError, ValueError) as exc:
            raise MalformedArgumentError(
                f"tg-export: --since {since_dir}: cannot read manifest.json: {exc}"
            ) from exc
        try:
            # A prior manifest that parses as JSON but is structurally wrong (not the
            # expected shape, or a chat entry lacking max_message_id) is still a usage
            # mistake, not a generic runtime crash — keep it on the greppable arg code.
            return {
                int(chat["id"]): int(chat["max_message_id"])
                for chat in manifest.get("chats", [])
            }
        except (KeyError, TypeError, AttributeError) as exc:
            raise MalformedArgumentError(
                f"tg-export: --since {since_dir}: manifest chat entry missing max_message_id"
            ) from exc

    # No manifest: recompute from the partial NDJSON (killed-run resume, M6).
    ndjson_anchors = _ndjson_anchors(since_dir)
    if ndjson_anchors is None:
        raise MalformedArgumentError(
            f"tg-export: --since {since_dir}: cannot read manifest.json: not found"
        )
    log_event("resume_from_partial", chats=len(ndjson_anchors))
    return ndjson_anchors


def _ndjson_anchors(since_dir: Path) -> dict[int, int] | None:
    """Recompute per-chat ``max_message_id`` anchors from on-disk NDJSON.

    Used only for the killed-run resume path, when the final manifest is missing.
    Returns ``chat id -> max written id`` for every ``chats/<id>.ndjson``, or
    ``None`` when there is nothing to resume from (no ``chats/`` dir or no files).
    A trailing partial line (a SIGKILL mid-write) is tolerated: it is skipped and
    the anchor is recomputed from the complete lines (best-effort, M6).
    """
    chats_dir = since_dir / "chats"
    if not chats_dir.is_dir():
        return None
    anchors: dict[int, int] = {}
    found = False
    for path in sorted(chats_dir.glob("*.ndjson")):
        found = True
        try:
            chat_id = int(path.stem)
        except ValueError:
            continue
        max_id = 0
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                log_event("resume_partial_line_skipped", chat=chat_id, line=lineno)
                continue
            mid = int(obj.get("id", 0) or 0)
            if mid > max_id:
                max_id = mid
        anchors[chat_id] = max_id
    return anchors if found else None


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
            # A corrupt prior line surfaces with chat/line context rather than a raw
            # decode error (SPEC-0001 REQ "Error Handling Standards"). This stays
            # loud on purpose: per-line flush means a clean SIGKILL leaves only
            # COMPLETE lines (the killed-run resume path in _ndjson_anchors tolerates
            # a trailing partial), so a corrupt MIDDLE line is genuine corruption the
            # caller must see, not a routine kill artifact.
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
    config: ExportConfig,
    min_id: int = 0,
    append: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """Iterate, map, validate, and append one chat's messages.

    Returns ``(mapped, skipped)`` — the list of messages actually written and the
    count of best-effort skips.

    ``min_id`` is the incremental lower bound threaded into ``iter_messages`` so a
    ``--since`` run fetches only messages with id greater than the prior anchor
    (ADR-0008). ``append`` opens the writer in ``"a"`` mode so those newer messages
    are appended past the chat's existing lines rather than truncating them; a full
    or new-chat run leaves ``append=False`` (the M3 truncate behavior).

    Reliability (M6, SPEC-0001 REQ "Reliability and Rate Limits"):

    * a ``FloodWaitError`` raised while iterating is slept (via
      :func:`reliability.sleep_flood`) and iteration RESUMES from ``last_id`` (the
      last id actually written) so nothing is re-emitted or lost — a flood-wait
      never fails the job;
    * a single message that fails to map, download, or validate is logged with
      ``chat <id>: message <id>`` context and SKIPPED with best-effort tolerance —
      it no longer aborts the chat (this deliberately replaces M3's per-message
      reject gate; schema validation stays a guard, but a failing message is
      skipped, not fatal).
    """
    mapped: list[dict[str, Any]] = []
    skipped = 0
    ndjson_path = output / "chats" / f"{chat_id}.ndjson"
    # Deliberate: opening the writer creates chats/<id>.ndjson eagerly, so an
    # in-scope chat with zero messages leaves a valid 0-byte file plus a manifest
    # entry (message_count 0). This keeps the manifest index and the on-disk file
    # set in lockstep and is what --since (M5) reopens to append to.
    # reverse=True => chronological (oldest-first) deterministic order (ADR-0003).
    with jsonio.ndjson_writer(ndjson_path, mode="a" if append else "w") as write_line:
        # last_id tracks the highest id actually WRITTEN; a flood-wait restart
        # resumes from it (min_id=last_id) so no line is duplicated or skipped.
        last_id = min_id
        while True:
            try:
                async for raw in client.iter_messages(
                    chat_id, reverse=True, min_id=last_id
                ):
                    raw_id = getattr(raw, "id", "?")
                    try:
                        obj = mapping.map_message(raw, chat_id=chat_id, self_id=self_id)
                        # M4 media seam: download (or skip-stub) the message's media
                        # and set media.path IN PLACE before validation/write, so the
                        # emitted line — and thus the golden — captures it (ADR-0003).
                        await media.download_message_media(
                            client, raw, obj, chat_id=chat_id, output=output, config=config
                        )
                        # Schema validation stays a guard; a failure now skips this
                        # one message (below) rather than aborting the chat.
                        schemas.validate("message", obj)
                    except FloodWaitError:
                        # NOT a skip: a flood-wait is survivable — bubble to the outer
                        # retry which sleeps and resumes iteration from last_id.
                        raise
                    except Exception as exc:  # noqa: BLE001 - best-effort tolerance (M6)
                        skipped += 1
                        log_event(
                            "message_skipped",
                            level=logging.WARNING,
                            chat=chat_id,
                            msg=raw_id,
                            cause=type(exc).__name__,
                        )
                        continue
                    write_line(obj)
                    mapped.append(obj)
                    last_id = int(obj["id"])
            except FloodWaitError as exc:
                await reliability.sleep_flood(exc, chat=chat_id)
                continue  # resume iteration from last_id (no dupes, no gaps)
            break
    return mapped, skipped


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
    skipped_messages_total = 0
    skipped_chats = 0
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
        try:
            new_messages, skipped = await _export_chat(
                client,
                chat_id=chat_id,
                chat_type=chat_type,
                self_id=self_id,
                output=output,
                config=config,
                min_id=min_id,
                append=append,
            )
        except (NotAuthorizedError, NetworkError, MalformedArgumentError):
            # Genuine auth/network/config failures still fail loudly — they are not
            # a "bad chat" the run should tolerate.
            raise
        except Exception as exc:  # noqa: BLE001 - best-effort per-chat tolerance (M6)
            # One bad chat (e.g. an inaccessible peer) is logged and skipped; it MUST
            # NOT abort the run (SPEC-0001 REQ "Reliability and Rate Limits").
            skipped_chats += 1
            log_event(
                "chat_error_skipped",
                level=logging.WARNING,
                chat=chat_id,
                type=chat_type,
                cause=type(exc).__name__,
            )
            continue
        skipped_messages_total += skipped
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
            skipped=skipped,
        )

    manifest = build_manifest(account, chat_entries, generated_at=generated_at)
    # Reject gate: the manifest must validate before it is written.
    schemas.validate("manifest", manifest)
    jsonio.write_manifest(output / "manifest.json", manifest)
    log_event(
        "export_complete",
        chats=len(chat_entries),
        messages=sum(entry["message_count"] for entry in chat_entries),
        skipped_chats=skipped_chats,
        skipped_messages=skipped_messages_total,
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
