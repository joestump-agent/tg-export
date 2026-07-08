"""Determinism is a hard requirement: msgbrowse content-hashes each message to
dedupe idempotent re-imports (ADR-0004, SPEC-0001 REQ "JSON Output Contract")."""

from __future__ import annotations

import json
from pathlib import Path

import synthetic
from tg_export import jsonio


def test_dumps_is_idempotent_and_key_order_independent():
    a = {"b": 1, "a": 2, "nested": {"y": 1, "x": 2}}
    b = {"a": 2, "nested": {"x": 2, "y": 1}, "b": 1}  # same data, different insertion order
    assert jsonio.dumps(a) == jsonio.dumps(b)
    # No insignificant whitespace, sorted keys.
    assert jsonio.dumps(a) == '{"a":2,"b":1,"nested":{"x":2,"y":1}}'


def test_ndjson_line_has_single_trailing_newline():
    line = jsonio.ndjson_line({"id": 1})
    assert line.endswith("\n")
    assert not line.rstrip("\n").endswith("\n")
    assert line.count("\n") == 1


def test_canonical_roundtrip_is_byte_stable():
    # Parsing a canonical line and re-serializing MUST reproduce it byte-for-byte.
    for message in synthetic.all_valid_messages():
        line = jsonio.dumps(message)
        assert jsonio.dumps(json.loads(line)) == line


def test_reexport_of_same_message_is_byte_identical(tmp_path: Path):
    # SPEC-0001 scenario: the same synthetic message exported twice into fresh
    # output dirs yields byte-for-byte identical NDJSON lines.
    one = tmp_path / "run1"
    two = tmp_path / "run2"
    synthetic.write_golden(one)
    synthetic.write_golden(two)
    for rel in ("chats/1001.ndjson", "chats/5005.ndjson", "manifest.json"):
        assert (one / rel).read_bytes() == (two / rel).read_bytes()


def test_ensure_ascii_false_preserves_utf8_bytes():
    # A given string always encodes to the same UTF-8 bytes (no \\uXXXX variance).
    obj = {"emoji": "\U0001f525", "heart": "❤"}
    assert jsonio.encode(obj) == '{"emoji":"🔥","heart":"❤"}'.encode()
