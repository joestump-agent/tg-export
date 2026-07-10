"""The tdl-raw -> contract transform: the new workhorse (ADR-0011).

tg-export no longer talks to Telegram. tdl performs the one-click, session-imported
export (its ``login -T desktop`` reuses an installed Telegram Desktop session — the
same "auth is the source app's job" pattern msgbrowse already uses for Signal,
iMessage, and WhatsApp), and hands off a ``--raw`` dump. This module turns that dump
into the msgbrowse JSON output contract:

1. load the tdl export into :class:`~tg_export.adapter.TdlExport`;
2. for each chat, reshape each raw message through
   :func:`tg_export.adapter.adapt_message`, map it with the unchanged
   :func:`tg_export.mapping.map_message`, validate it against the shipped schema,
   and append it to ``chats/<chat_id>.ndjson`` as it is produced (ADR-0003);
3. assemble and write the validated ``manifest.json`` last.

The pure transform is offline and deterministic: no network, no clock beyond the
manifest ``generated_at`` (a test/reproducibility seam). Byte-stability (ADR-0004)
is inherited from :mod:`tg_export.jsonio`.

Incrementality moved upstream: tdl's time-window refresh produces the input, and
msgbrowse content-hashes each message for idempotent import, so this transform is
stateless — it renders whatever tdl dumped.

# Governing: ADR-0003 (contract), ADR-0004 (determinism), ADR-0011 (transform
#            pivot); SPEC-0001 REQ "JSON Output Contract", "CLI Surface"
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import adapter, archive, jsonio, mapping, schemas
from .errors import MalformedInputError, TransformError
from .logging import log_event


@dataclass
class TransformConfig:
    """Everything the transform needs, resolved from the CLI surface.

    ``generated_at`` pins the manifest timestamp for reproducible fixtures; a real
    run leaves it ``None`` and stamps wall-clock time.
    """

    input: Path
    output: Path
    generated_at: int | None = None


def _load_tdl_export(input_path: Path) -> adapter.TdlExport:
    """Read a tdl export off disk into a :class:`~tg_export.adapter.TdlExport`.

    TODO(tdl-shape) VERIFICATION GATE (ADR-0011): the on-disk structure below is the
    ASSUMED shape of a ``tdl chat export`` tree/file and MUST be confirmed against a
    real dump. The assumed top-level shape is a JSON document::

        {"account": {...}, "chats": [{"id", "type", "title", "username",
                                       "messages": [ <raw msg>, ... ]}, ...],
         "entities": {"<peer id>": {"name", "username"}}}

    A missing/unreadable/badly-shaped input is a caller-facing usage failure
    (:class:`MalformedInputError`), distinct from a genuine transform crash.
    """
    if not input_path.exists():
        raise MalformedInputError(f"tg-export: input not found: {input_path}")
    doc_path = input_path / "tdl-export.json" if input_path.is_dir() else input_path
    try:
        doc = json.loads(doc_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MalformedInputError(
            f"tg-export: cannot read tdl export at {doc_path}: {exc}"
        ) from exc
    return export_from_doc(doc)


def export_from_doc(doc: dict[str, Any]) -> adapter.TdlExport:
    """Build a :class:`~tg_export.adapter.TdlExport` from a parsed tdl document.

    Split out from file IO so tests drive the transform from an in-memory dict.
    """
    try:
        account = dict(doc["account"])
        entity_index = {
            int(pid): dict(meta) for pid, meta in (doc.get("entities") or {}).items()
        }
        chats = [
            adapter.TdlChat(
                id=int(c["id"]),
                type=str(c["type"]),
                title=str(c.get("title") or c["id"]),
                username=c.get("username"),
                raw_messages=list(c.get("messages") or []),
            )
            for c in doc.get("chats") or []
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise MalformedInputError(f"tg-export: malformed tdl export: {exc}") from exc
    return adapter.TdlExport(
        account=account,
        self_id=int(account["id"]) if account.get("id") is not None else None,
        chats=chats,
        entity_index=entity_index,
    )


def transform_export(export: adapter.TdlExport, config: TransformConfig) -> dict[str, Any]:
    """Render a loaded tdl export into the archive; return the written manifest.

    This is the seam tests drive directly with an in-memory export.
    """
    output = Path(config.output)
    generated_at = config.generated_at if config.generated_at is not None else int(time.time())
    account = archive.account_block(export.account)
    self_id = export.self_id

    chat_entries: list[dict[str, Any]] = []
    for chat in export.chats:
        ndjson_path = output / "chats" / f"{chat.id}.ndjson"
        mapped: list[dict[str, Any]] = []
        with jsonio.ndjson_writer(ndjson_path) as write_line:
            for raw in chat.raw_messages:
                raw_id = raw.get("id", "?")
                try:
                    adapted = adapter.adapt_message(raw, entity_index=export.entity_index)
                    obj = mapping.map_message(adapted, chat_id=chat.id, self_id=self_id)
                    schemas.validate("message", obj)
                except Exception as exc:  # noqa: BLE001 - wrap with chat/msg context
                    raise TransformError(
                        f"chat {chat.id}: message {raw_id}: {exc}", chat=chat.id
                    ) from exc
                write_line(obj)
                mapped.append(obj)
        chat_entries.append(
            archive.chat_manifest_entry(chat.id, chat.type, chat.title, chat.username, mapped)
        )
        log_event("chat_transformed", chat=chat.id, type=chat.type, messages=len(mapped))

    manifest = archive.build_manifest(account, chat_entries, generated_at=generated_at)
    schemas.validate("manifest", manifest)
    jsonio.write_manifest(output / "manifest.json", manifest)
    log_event(
        "transform_complete",
        chats=len(chat_entries),
        messages=sum(entry["message_count"] for entry in chat_entries),
    )
    return manifest


def run_transform(config: TransformConfig) -> dict[str, Any]:
    """Load the tdl export named by ``config.input`` and transform it."""
    export = _load_tdl_export(Path(config.input))
    return transform_export(export, config)


__all__ = [
    "TransformConfig",
    "export_from_doc",
    "run_transform",
    "transform_export",
]
