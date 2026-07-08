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
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
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


def read_manifest(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read and parse a ``manifest.json`` written by :func:`write_manifest`.

    The counterpart the incremental ``--since`` path (M5, ADR-0008) uses to recover
    the prior run's per-chat ``max_message_id`` anchors. Raises ``OSError`` if the
    file is absent and ``ValueError`` (``json.JSONDecodeError``) if it is malformed;
    the caller wraps those into a stable, greppable argument error.
    """
    return json.loads(Path(path).read_text(encoding="utf-8"))


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


@contextmanager
def ndjson_writer(
    path: str | os.PathLike[str], *, mode: str = "w"
) -> Iterator[Callable[[Any], None]]:
    """Yield an append-as-you-go writer for one chat's NDJSON file.

    The exporter walks a chat and writes each mapped message the moment it is
    produced (ADR-0003): the yielded callable serializes one object to one
    canonical line and flushes it, so a killed run leaves a valid, truncated-at-a-
    line-boundary partial tree that ``--since`` can resume (M5). ``mode`` defaults
    to ``"w"`` (a fresh full run truncates any prior file); the incremental path
    (M5) opens with ``"a"`` to append past a chat's existing lines. Every line goes
    through :func:`ndjson_line`, so byte-stability (ADR-0004) is identical to the
    batch writer.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fh = p.open(mode, encoding="utf-8", newline="\n")
    try:

        def write(obj: Any) -> None:
            fh.write(ndjson_line(obj))
            # Flush per line so a killed run never leaves a half-written record.
            fh.flush()

        yield write
    finally:
        fh.close()
