"""Canonical, deterministic JSON serialization for the export contract.

Every milestone depends on byte-identical output: msgbrowse content-hashes each
message to dedupe idempotent re-imports, so re-exporting the same message MUST
produce byte-for-byte identical bytes (ADR-0004, SPEC-0001 REQ "JSON Output
Contract"). This module is the single place that turns contract dicts into bytes.

Byte-stability is guaranteed by:
  * ``sort_keys=True`` — key order never depends on dict insertion order;
  * ``separators=(",", ":")`` — no whitespace, no run-varying formatting;
  * ``ensure_ascii=False`` — a given string always encodes to the same UTF-8 bytes
    (no ``\\uXXXX`` escaping that could vary), written as UTF-8;
  * a single trailing ``\\n`` per NDJSON line and per manifest file.

Callers MUST NOT introduce run-varying fields (timestamps of the run, absolute
paths, etc.) into the objects they pass here — determinism is a property of the
data as much as the encoder.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# Governing: ADR-0004 (determinism); SPEC-0001 REQ "JSON Output Contract"

_JSON_KWARGS: dict[str, Any] = {
    "sort_keys": True,
    "ensure_ascii": False,
    "separators": (",", ":"),
}


def dumps(obj: Any) -> str:
    """Serialize ``obj`` to a canonical, compact, sorted-key JSON string.

    The result contains no insignificant whitespace and no trailing newline.
    Re-serializing an equal object always yields an identical string.
    """
    return json.dumps(obj, **_JSON_KWARGS)


def ndjson_line(obj: Any) -> str:
    """Serialize ``obj`` to one canonical NDJSON line (canonical JSON + ``\\n``)."""
    return dumps(obj) + "\n"


def encode(obj: Any) -> bytes:
    """Serialize ``obj`` to canonical UTF-8 bytes (no trailing newline)."""
    return dumps(obj).encode("utf-8")


def write_manifest(path: str | os.PathLike[str], manifest: dict[str, Any]) -> None:
    """Write ``manifest`` as a canonical JSON document with a trailing newline."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dumps(manifest) + "\n", encoding="utf-8")


def write_ndjson(path: str | os.PathLike[str], objects: Iterable[Any]) -> int:
    """Write ``objects`` as canonical NDJSON, one object per line.

    Returns the number of lines written. Overwrites any existing file; the
    append-as-you-go path used by the exporter is added in a later milestone.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with p.open("w", encoding="utf-8", newline="\n") as fh:
        for obj in objects:
            fh.write(ndjson_line(obj))
            count += 1
    return count
