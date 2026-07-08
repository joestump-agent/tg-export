"""Incremental ``--since`` / ``--full`` export tests — offline, 100% synthetic.

Drives :func:`tg_export.export.export_with_client` against a fake client to prove
the SPEC-0001 REQ "Incremental Export" contract (ADR-0008):

* a ``--since`` run reads the prior manifest's per-chat ``max_message_id`` and
  threads it as ``iter_messages(min_id=...)`` so only newer messages are fetched
  and appended in place (prior lines preserved);
* a chat absent from the prior manifest exports in full on the since-run;
* ``--full`` ignores all anchors and re-exports everything;
* re-emitting a boundary message is byte-identical (idempotent on msgbrowse's
  side, per ADR-0004);
* the fresh manifest is complete and schema-valid.

# Governing: SPEC-0001 REQ "Incremental Export"; ADR-0008, ADR-0004
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from synthetic import GENERATED_AT, FakeTelegramClient, Msg, User, _dt
from tg_export import export, jsonio, mapping, schemas
from tg_export.errors import ExportError, MalformedArgumentError

SELF_ID = 424242
ACCOUNT = {"id": SELF_ID, "username": "trailmix", "phone_last4": "6789"}
CAM = User(900003, "Cam", "Reyes", username="camreyes")


def _msg(mid: int) -> Msg:
    """A minimal, deterministic synthetic message with monotonic id/date."""
    return Msg(id=mid, date=_dt(1719800000 + mid), message=f"note {mid}", sender=CAM)


def _chat_meta(chat_id: int, messages: list[Msg]) -> dict[str, Any]:
    return {
        "type": "supergroup",
        "title": f"Chat {chat_id}",
        "username": None,
        "entity_id": chat_id,
        "megagroup": True,
        "broadcast": False,
        "is_user": False,
        "messages": messages,
    }


class RecordingClient(FakeTelegramClient):
    """A fake client that records the ``min_id`` each chat's walk was invoked with."""

    def __init__(self, chats: dict[int, dict[str, Any]]) -> None:
        super().__init__(chats, ACCOUNT)
        self.min_ids: dict[int, int] = {}

    async def iter_messages(self, chat_id: int, *, min_id: int = 0, **kw: Any):
        self.min_ids[chat_id] = min_id
        async for m in super().iter_messages(chat_id, min_id=min_id, **kw):
            yield m


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _read_objs(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in _read_lines(path)]


def _run(client: Any, config: export.ExportConfig) -> dict[str, Any]:
    return asyncio.run(export.export_with_client(client, config))


# --- (a) since-run fetches only newer messages and appends in place ----------


def test_since_run_passes_min_id_and_appends_only_new(tmp_path: Path):
    tree = tmp_path / "tree"
    # First full run: chat 3003 has ids [1,2,3] -> anchor becomes 3.
    first = RecordingClient({3003: _chat_meta(3003, [_msg(1), _msg(2), _msg(3)])})
    m1 = _run(first, export.ExportConfig(output=tree, generated_at=GENERATED_AT))
    assert m1["chats"][0]["max_message_id"] == 3
    assert first.min_ids[3003] == 0  # full run: no lower bound

    ndjson = tree / "chats" / "3003.ndjson"
    original = _read_lines(ndjson)
    assert len(original) == 3

    # Second run --since: two new messages arrive (ids 4,5).
    second = RecordingClient(
        {3003: _chat_meta(3003, [_msg(1), _msg(2), _msg(3), _msg(4), _msg(5)])}
    )
    m2 = _run(second, export.ExportConfig(output=tree, since=tree, generated_at=GENERATED_AT))

    # Only messages with id > M(=3) were fetched.
    assert second.min_ids[3003] == 3
    lines = _read_lines(ndjson)
    assert lines[:3] == original  # existing lines preserved byte-for-byte
    assert len(lines) == 5  # two new lines appended
    ids = [o["id"] for o in _read_objs(ndjson)]
    assert ids == [1, 2, 3, 4, 5]
    for obj in _read_objs(ndjson):
        schemas.validate("message", obj)

    # The fresh manifest reflects the COMPLETE file (count + new anchor + dates).
    entry = m2["chats"][0]
    assert entry["message_count"] == 5
    assert entry["max_message_id"] == 5
    schemas.validate("manifest", m2)


# --- (b) a new chat (absent from prior manifest) exports in full -------------


def test_new_chat_exports_in_full_on_since_run(tmp_path: Path):
    tree = tmp_path / "tree"
    first = RecordingClient({3003: _chat_meta(3003, [_msg(1), _msg(2)])})
    _run(first, export.ExportConfig(output=tree, generated_at=GENERATED_AT))

    # Second run: 3003 unchanged; brand-new chat 4004 appears with ids [1,2,3].
    second = RecordingClient(
        {
            3003: _chat_meta(3003, [_msg(1), _msg(2)]),
            4004: _chat_meta(4004, [_msg(1), _msg(2), _msg(3)]),
        }
    )
    m2 = _run(second, export.ExportConfig(output=tree, since=tree, generated_at=GENERATED_AT))

    # Existing chat resumed from its anchor; new chat walked in full (min_id 0).
    assert second.min_ids[3003] == 2
    assert second.min_ids[4004] == 0
    new_entry = next(c for c in m2["chats"] if c["id"] == 4004)
    assert new_entry["message_count"] == 3
    objs = _read_objs(tree / "chats" / "4004.ndjson")
    assert [o["id"] for o in objs] == [1, 2, 3]


# --- (c) --full ignores anchors and re-exports everything --------------------


def test_full_override_ignores_anchors(tmp_path: Path):
    tree = tmp_path / "tree"
    first = RecordingClient({3003: _chat_meta(3003, [_msg(1), _msg(2), _msg(3)])})
    _run(first, export.ExportConfig(output=tree, generated_at=GENERATED_AT))
    ndjson = tree / "chats" / "3003.ndjson"
    assert len(_read_lines(ndjson)) == 3

    # --full (even with --since given): anchors ignored, min_id 0, file re-truncated.
    second = RecordingClient({3003: _chat_meta(3003, [_msg(1), _msg(2), _msg(3)])})
    _run(
        second,
        export.ExportConfig(output=tree, since=tree, full=True, generated_at=GENERATED_AT),
    )
    assert second.min_ids[3003] == 0  # anchor (3) ignored
    assert len(_read_lines(ndjson)) == 3  # re-exported, not doubled


# --- (d) boundary re-emit is byte-identical (idempotent, ADR-0004) -----------


def test_boundary_reemit_is_byte_identical(tmp_path: Path):
    tree = tmp_path / "tree"
    (tree / "chats").mkdir(parents=True)
    ndjson = tree / "chats" / "3003.ndjson"

    # Seed a prior tree whose file already holds [1,2,3] but whose manifest anchor is
    # deliberately one behind (2) — simulating a boundary overlap. The since-run then
    # re-fetches id 3 (> 2) and appends it a second time; msgbrowse dedupes it.
    msgs = [_msg(1), _msg(2), _msg(3)]
    mapped = [mapping.map_message(m, chat_id=3003, self_id=SELF_ID) for m in msgs]
    jsonio.write_ndjson(ndjson, mapped)
    stale_manifest = export.build_manifest(
        export.account_block(ACCOUNT),
        [export.chat_manifest_entry(3003, "supergroup", "Chat 3003", None, mapped[:2])],
        generated_at=GENERATED_AT,
    )
    # Force the anchor behind the true max (2 instead of 3).
    stale_manifest["chats"][0]["max_message_id"] = 2
    jsonio.write_manifest(tree / "manifest.json", stale_manifest)
    original_line3 = _read_lines(ndjson)[2]

    client = RecordingClient({3003: _chat_meta(3003, msgs)})
    _run(client, export.ExportConfig(output=tree, since=tree, generated_at=GENERATED_AT))

    assert client.min_ids[3003] == 2
    lines = _read_lines(ndjson)
    assert len(lines) == 4  # id 3 re-emitted at the boundary
    reemitted = lines[3]
    # The re-emitted boundary line is byte-for-byte identical to the original.
    assert reemitted == original_line3
    assert json.loads(reemitted)["id"] == 3


# --- (e) fresh manifest is complete and schema-valid on a since-run ----------


def test_since_manifest_is_complete_and_valid(tmp_path: Path):
    tree = tmp_path / "tree"
    first = RecordingClient({3003: _chat_meta(3003, [_msg(1)])})
    _run(first, export.ExportConfig(output=tree, generated_at=GENERATED_AT))

    second = RecordingClient({3003: _chat_meta(3003, [_msg(1), _msg(2)])})
    m2 = _run(second, export.ExportConfig(output=tree, since=tree, generated_at=GENERATED_AT))

    on_disk = json.loads((tree / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk == m2
    schemas.validate("manifest", on_disk)
    assert on_disk["schema_version"] == 1
    assert on_disk["tool"] == "tg-export"
    entry = on_disk["chats"][0]
    assert entry["message_count"] == 2
    assert entry["max_message_id"] == 2


# --- missing prior manifest is a stable malformed-argument error -------------


def test_since_missing_manifest_raises_malformed_argument(tmp_path: Path):
    client = RecordingClient({3003: _chat_meta(3003, [_msg(1)])})
    with pytest.raises(MalformedArgumentError) as exc:
        _run(client, export.ExportConfig(output=tmp_path / "out", since=tmp_path / "nope"))
    assert "--since" in str(exc.value)


def test_since_structurally_broken_manifest_is_malformed_argument(tmp_path: Path):
    # Valid JSON, but a chat entry lacks max_message_id -> a KeyError must NOT escape
    # to the generic runtime code; it stays a greppable malformed-argument error.
    since = tmp_path / "prev"
    since.mkdir()
    jsonio.write_manifest(
        since / "manifest.json",
        {
            "schema_version": 1,
            "tool": "tg-export",
            "chats": [{"id": 3003, "type": "supergroup"}],  # no max_message_id
        },
    )
    client = RecordingClient({3003: _chat_meta(3003, [_msg(1)])})
    with pytest.raises(MalformedArgumentError) as exc:
        _run(client, export.ExportConfig(output=since, since=since))
    assert "max_message_id" in str(exc.value)


def test_corrupt_prior_ndjson_line_surfaces_contextual_error(tmp_path: Path):
    # A half-written/corrupt prior line (e.g. after a SIGKILL) must surface with
    # chat + line-number context, not a bare JSONDecodeError (M6 upgrades to skip).
    tree = tmp_path / "tree"
    first = RecordingClient({3003: _chat_meta(3003, [_msg(1), _msg(2)])})
    _run(first, export.ExportConfig(output=tree, generated_at=GENERATED_AT))

    ndjson = tree / "chats" / "3003.ndjson"
    # Replace the second prior line with a truncated (corrupt) JSON object.
    good_first = _read_lines(ndjson)[0]
    ndjson.write_text(good_first + "\n" + '{"id": 2, "chat_id"\n', encoding="utf-8")

    second = RecordingClient({3003: _chat_meta(3003, [_msg(1), _msg(2), _msg(3)])})
    with pytest.raises(ExportError) as exc:
        _run(second, export.ExportConfig(output=tree, since=tree, generated_at=GENERATED_AT))
    message = str(exc.value)
    assert "chat 3003:" in message
    assert "line 2" in message
    assert "malformed" in message
