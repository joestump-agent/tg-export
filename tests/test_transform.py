"""Transform-pipeline tests (ADR-0011; SPEC-0001 REQ "JSON Output Contract").

These drive the production ``adapter -> mapping -> schema -> archive`` path over an
IN-MEMORY tdl-raw document, offline. The document's shape is the ASSUMED tdl ``--raw``
structure (VERIFICATION GATE, ADR-0011); when a real ``tdl chat export --raw`` dump is
in hand, this fixture is replaced with a captured one and the adapter's fidelity
fields are wired in. Until then these lock in the wiring: every emitted message and
the manifest validate against the shipped schema, senders resolve through the entity
index (and degrade to id-only when absent), and output is byte-stable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tg_export import jsonio, schemas, transform
from tg_export.errors import MalformedInputError

GENERATED_AT = 1719900000

# The assumed tdl `--raw` document shape (see adapter/transform TODO(tdl-shape)).
TDL_DOC = {
    "account": {"id": 424242, "username": "trailmix"},
    "entities": {
        "900001": {"name": "Ada Copeland", "username": "adacope"},
    },
    "chats": [
        {
            "id": 1001,
            "type": "supergroup",
            "title": "Weekend Hikers",
            "username": "weekendhikers",
            "messages": [
                {"id": 10, "date": 1719792000, "text": "Anyone up for the ridge loop?",
                 "from_id": 900001},
                # from_id as a raw Peer wrapper; sender not in the entity index -> id-only.
                {"id": 11, "date": 1719792600, "text": "I'm in!",
                 "from_id": {"user_id": 900002}, "reply_to_message_id": 10},
                # no from_id at all -> Unknown / null id (the documented degrade).
                {"id": 12, "date": 1719793200, "text": "no sender here"},
            ],
        },
    ],
}


def _transform_doc(tmp_path: Path) -> dict:
    export = transform.export_from_doc(TDL_DOC)
    config = transform.TransformConfig(
        input=tmp_path, output=tmp_path, generated_at=GENERATED_AT
    )
    return transform.transform_export(export, config)


def test_manifest_is_valid_and_indexed(tmp_path: Path):
    manifest = _transform_doc(tmp_path)
    schemas.validate("manifest", manifest)
    assert manifest["schema_version"] == 1
    assert manifest["tool"] == "tg-export"
    assert manifest["account"] == {"id": 424242, "username": "trailmix", "phone_last4": None}
    (entry,) = manifest["chats"]
    assert entry["message_count"] == 3
    assert entry["max_message_id"] == 12
    assert entry["file"] == "chats/1001.ndjson"


def test_every_written_message_validates(tmp_path: Path):
    _transform_doc(tmp_path)
    lines = (tmp_path / "chats" / "1001.ndjson").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    for line in lines:
        schemas.validate("message", json.loads(line))


def test_sender_resolves_through_entity_index(tmp_path: Path):
    _transform_doc(tmp_path)
    lines = (tmp_path / "chats" / "1001.ndjson").read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    assert first["from"] == {
        "id": 900001,
        "is_self": False,
        "name": "Ada Copeland",
        "username": "adacope",
    }
    assert first["text"].startswith("Anyone up")


def test_unresolved_sender_degrades_to_id_or_unknown(tmp_path: Path):
    _transform_doc(tmp_path)
    lines = (tmp_path / "chats" / "1001.ndjson").read_text(encoding="utf-8").splitlines()
    # from_id present but not in the entity index -> id kept, name Unknown.
    second = json.loads(lines[1])
    assert second["from"]["id"] == 900002
    assert second["reply_to_message_id"] == 10
    # no from_id at all -> fully unresolved.
    third = json.loads(lines[2])
    assert third["from"] == {"name": "Unknown", "is_self": False, "id": None}


def test_output_is_byte_stable(tmp_path: Path):
    one, two = tmp_path / "a", tmp_path / "b"
    for out in (one, two):
        export = transform.export_from_doc(TDL_DOC)
        transform.transform_export(
            export, transform.TransformConfig(input=out, output=out, generated_at=GENERATED_AT)
        )
    for rel in ("chats/1001.ndjson", "manifest.json"):
        assert (one / rel).read_bytes() == (two / rel).read_bytes()


def test_run_transform_reads_a_file(tmp_path: Path):
    src = tmp_path / "tdl-export.json"
    src.write_text(jsonio.dumps(TDL_DOC), encoding="utf-8")
    out = tmp_path / "archive"
    manifest = transform.run_transform(
        transform.TransformConfig(input=src, output=out, generated_at=GENERATED_AT)
    )
    assert manifest["chats"][0]["message_count"] == 3
    assert (out / "manifest.json").exists()


def test_missing_input_raises_malformed_input(tmp_path: Path):
    with pytest.raises(MalformedInputError):
        transform.run_transform(
            transform.TransformConfig(input=tmp_path / "nope.json", output=tmp_path / "o")
        )


def test_malformed_document_raises_malformed_input():
    with pytest.raises(MalformedInputError):
        transform.export_from_doc({"chats": [{"no": "account"}]})
