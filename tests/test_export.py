"""End-to-end export walk tests — offline, mocked Telethon, 100% synthetic.

Drives :func:`tg_export.export.export_with_client` against the fake client and
asserts the SPEC-0001 tree contract: every emitted object validates against the
shipped schema, the default scope excludes channels while ``--chats`` opts one in
(ADR-0007), re-export is byte-identical (ADR-0004), the schema reject gate fails
loudly, and progress logs never carry a message body.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

import synthetic
from synthetic import FakeTelegramClient
from tg_export import export, mapping, schemas
from tg_export.errors import ExportError


def _run(config: export.ExportConfig) -> dict:
    client = FakeTelegramClient()
    return asyncio.run(export.export_with_client(client, config))


def _read_ndjson(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --- the SPEC-0001 tree validates --------------------------------------------


def test_full_export_tree_validates_against_schema(tmp_path: Path):
    manifest = _run(export.ExportConfig(output=tmp_path, generated_at=synthetic.GENERATED_AT))
    schemas.validate("manifest", manifest)

    on_disk = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    schemas.validate("manifest", on_disk)
    assert on_disk == manifest

    total = 0
    for entry in manifest["chats"]:
        for obj in _read_ndjson(tmp_path / entry["file"]):
            schemas.validate("message", obj)
            total += 1
    assert total == sum(e["message_count"] for e in manifest["chats"])


def test_export_run_matches_committed_golden(tmp_path: Path, golden_dir: Path):
    # The real export walk (not just write_golden) reproduces the committed tree.
    _run(export.ExportConfig(output=tmp_path, generated_at=synthetic.GENERATED_AT))
    for rel in ("manifest.json", "chats/1001.ndjson", "chats/5005.ndjson"):
        assert (tmp_path / rel).read_bytes() == (golden_dir / rel).read_bytes()


# --- scope: ADR-0007 ---------------------------------------------------------


def test_default_scope_excludes_channels(tmp_path: Path):
    manifest = _run(export.ExportConfig(output=tmp_path))
    ids = [c["id"] for c in manifest["chats"]]
    assert ids == [1001, 5005]  # the broadcast channel 2002 is excluded
    assert not (tmp_path / "chats" / "2002.ndjson").exists()


def test_chats_filter_opts_in_a_channel(tmp_path: Path):
    manifest = _run(export.ExportConfig(output=tmp_path, chats=frozenset({2002})))
    assert [c["id"] for c in manifest["chats"]] == [2002]
    entry = manifest["chats"][0]
    assert entry["type"] == "channel"
    # Its posts are still emitted and valid.
    objs = _read_ndjson(tmp_path / "chats" / "2002.ndjson")
    for obj in objs:
        schemas.validate("message", obj)
        assert obj["from"]["id"] is None  # anonymous channel posts


def test_empty_account_writes_valid_empty_manifest(tmp_path: Path):
    client = FakeTelegramClient(chats={}, account=synthetic.ACCOUNT)
    manifest = asyncio.run(
        export.export_with_client(client, export.ExportConfig(output=tmp_path))
    )
    assert manifest["chats"] == []
    schemas.validate("manifest", manifest)
    assert not (tmp_path / "chats").exists()


# --- determinism: ADR-0004 ---------------------------------------------------


def test_reexport_is_byte_identical(tmp_path: Path):
    one = tmp_path / "run1"
    two = tmp_path / "run2"
    _run(export.ExportConfig(output=one, generated_at=synthetic.GENERATED_AT))
    _run(export.ExportConfig(output=two, generated_at=synthetic.GENERATED_AT))
    for rel in ("chats/1001.ndjson", "chats/5005.ndjson", "manifest.json"):
        assert (one / rel).read_bytes() == (two / rel).read_bytes()


# --- reject gate: SPEC-0001 "JSON Output Contract" / "Error Handling" ---------


def test_invalid_mapped_object_fails_loudly_with_context(tmp_path: Path, monkeypatch):
    # An object that fails the shipped schema must abort the run with layer-boundary
    # context — never be silently written.
    def _bad(msg, *, chat_id, self_id):
        return {"id": msg.id, "chat_id": chat_id, "kind": "bogus"}  # invalid kind + missing fields

    monkeypatch.setattr(mapping, "map_message", _bad)
    with pytest.raises(ExportError) as exc:
        _run(export.ExportConfig(output=tmp_path))
    message = str(exc.value)
    assert "chat 1001:" in message
    assert "message 10:" in message
    assert "validation failed" in message


def test_mapping_exception_wrapped_with_context(tmp_path: Path, monkeypatch):
    def _boom(msg, *, chat_id, self_id):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(mapping, "map_message", _boom)
    with pytest.raises(ExportError) as exc:
        _run(export.ExportConfig(output=tmp_path))
    assert "mapping failed: kaboom" in str(exc.value)
    assert "chat 1001: message 10:" in str(exc.value)


# --- logging hygiene: SPEC-0001 "Error Handling Standards" --------------------


def test_progress_logs_carry_no_message_bodies(tmp_path: Path, caplog):
    caplog.set_level(logging.DEBUG, logger="tg_export")
    _run(export.ExportConfig(output=tmp_path, generated_at=synthetic.GENERATED_AT))
    text = caplog.text
    # Structured progress is present...
    assert "event=chat_exported" in text
    assert "event=export_complete" in text
    assert "event=chat_skipped" in text  # the excluded channel is logged
    # ...but no message body ever appears.
    for body in (
        "Anyone up for the ridge loop",
        "Remember to charge the GPS",
        "Trailhead road closed",
    ):
        assert body not in text


# --- discovery listing lists ALL dialogs incl. channels ----------------------


def test_list_chats_includes_channels():
    client = FakeTelegramClient()
    listing = asyncio.run(export.list_chats_with_client(client))
    by_id = {c["id"]: c for c in listing}
    assert set(by_id) == {1001, 2002, 5005}
    assert by_id[2002]["type"] == "channel"
    assert by_id[5005]["type"] == "self"
    assert by_id[1001]["type"] == "supergroup"


@pytest.mark.parametrize(
    "chat_type, expected",
    [
        ("private", "private"),
        ("group", "group"),
        ("supergroup", "supergroup"),
        ("channel", "channel"),
        ("self", "self"),
    ],
)
def test_classify_dialog_all_types(chat_type, expected):
    meta = {
        "type": chat_type,
        "title": "X",
        "username": None,
        "entity_id": synthetic.SELF_ID if chat_type == "self" else 555,
        "megagroup": chat_type == "supergroup",
        "broadcast": chat_type == "channel",
        "is_user": chat_type in ("private", "self"),
    }
    dialog = synthetic.FakeDialog(999, meta)
    assert export.classify_dialog(dialog, synthetic.SELF_ID) == expected
