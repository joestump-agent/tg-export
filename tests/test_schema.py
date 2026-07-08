"""The shipped JSON Schema is the executable contract (ADR-0004, SPEC-0001)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

import synthetic
from tg_export import schemas

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_schemas_are_valid_draft_2020_12():
    for name in schemas.SCHEMA_FILES:
        schema = schemas.load_schema(name)
        # Raises if the schema itself is malformed against the meta-schema.
        Draft202012Validator.check_schema(schema)
        assert schema["$schema"].endswith("2020-12/schema")


def test_packaged_and_repo_root_schemas_are_byte_identical():
    # The two copies (importable package data + repo-root schema/) MUST NOT drift.
    for filename in schemas.SCHEMA_FILES.values():
        packaged = (REPO_ROOT / "src" / "tg_export" / "schema" / filename).read_bytes()
        repo_root = (REPO_ROOT / "schema" / filename).read_bytes()
        assert packaged == repo_root, f"{filename} differs between package and repo root"


def test_golden_manifest_validates(golden_dir: Path):
    manifest = json.loads((golden_dir / "manifest.json").read_text(encoding="utf-8"))
    schemas.validate("manifest", manifest)


def test_golden_messages_validate(golden_dir: Path):
    count = 0
    for ndjson in sorted((golden_dir / "chats").glob("*.ndjson")):
        for line in ndjson.read_text(encoding="utf-8").splitlines():
            schemas.validate("message", json.loads(line))
            count += 1
    # 8 in chat 1001 + 2 in chat 5005.
    assert count == 10


@pytest.mark.parametrize(
    "message",
    [msg for _, msg in synthetic.MALFORMED_MESSAGES],
    ids=[desc for desc, _ in synthetic.MALFORMED_MESSAGES],
)
def test_malformed_messages_are_rejected(message):
    with pytest.raises(ValidationError):
        schemas.validate("message", message)


def test_empty_account_manifest_validates():
    # SPEC-0001: an account with no exportable chats still produces a valid manifest.
    manifest = synthetic.build_manifest()
    manifest["chats"] = []
    schemas.validate("manifest", manifest)


def test_phone_last4_over_four_chars_is_rejected():
    manifest = synthetic.build_manifest()
    manifest["account"]["phone_last4"] = "5551234567"
    with pytest.raises(ValidationError):
        schemas.validate("manifest", manifest)
