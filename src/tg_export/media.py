"""Media download + relative-path assignment (SPEC-0001 REQ "Media Handling").

M3 leaves every ``media`` block as metadata only, with ``path: null`` (see
:func:`tg_export.mapping.map_media`). M4 plugs the download into that seam: for a
message that carries a downloadable file, this module downloads it via the client
to ``media/<chat_id>/<message_id>[_<n>].<ext>`` and sets the *same* relative path
back onto the ``media`` object BEFORE the NDJSON line is written, so the on-disk
reference and the emitted contract always agree (ADR-0003).

The four knobs the REQ mandates all live here:

* **``--no-media``** — download nothing; the ``media`` metadata object is still
  emitted, ``path`` stays ``null`` and NO ``skipped`` flag is added.
* **``--max-media-mb N``** — a file larger than N MiB is NOT downloaded; the
  ``media`` object is still emitted with ``path: null`` AND ``skipped: true`` (an
  honest skip-stub).
* **idempotent re-download** — if the target file already exists at the expected
  size (the metadata ``size``), the file is not re-fetched; ``path`` is still set.
* **relative, deterministic paths** — the path is always POSIX-relative
  (``media/<chat_id>/...``), never absolute and never run-varying, so re-export is
  byte-stable (ADR-0004).

A download error is NOT silently swallowed: it is wrapped with layer-boundary
context (``chat <id>: message <id>: media download failed: <cause>``) and raised.
Per-message best-effort *tolerance* (never abort the chat on one bad file) is M6.

# Governing: SPEC-0001 REQ "Media Handling", REQ "Error Handling Standards";
#            ADR-0003 (media/<chat_id>/<message_id>[_<n>].<ext>), ADR-0004
"""

from __future__ import annotations

import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import ExportError
from .logging import log_event

# --- extension derivation ----------------------------------------------------

#: MIME -> file extension for the types tg-export commonly downloads. Kept as an
#: explicit map (not ``mimetypes.guess_extension``) so the chosen extension is
#: deterministic and identical across platforms (ADR-0004).
_MIME_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
}

#: A derived extension is accepted from an untrusted Telegram filename only if it
#: matches this short, portable pattern (letters/digits, 1-10 chars, after the dot).
#: Anything else — backslashes, tabs/control chars, RTL-override unicode, an
#: over-long suffix — is rejected so neither the on-disk name nor the emitted
#: ``media.path`` (consumed cross-platform by msgbrowse) can carry hostile bytes.
_SAFE_EXT_RE = re.compile(r"^[a-z0-9]{1,10}$")

#: Kind -> extension fallback when neither a filename nor a known MIME resolves.
_KIND_EXT: dict[str, str] = {
    "photo": ".jpg",
    "video": ".mp4",
    "video_note": ".mp4",
    "voice": ".ogg",
    "audio": ".mp3",
    "sticker": ".webp",
    "animation": ".mp4",
    "document": ".bin",
}


def _extension_for(media_obj: dict[str, Any]) -> str:
    """Pick a deterministic file extension for a mapped media object.

    Precedence: the original filename's suffix (cheapest, most faithful) → a known
    MIME → the kind fallback → ``.bin``. The filename is UNTRUSTED input from
    Telegram, so its suffix is only accepted when it matches ``_SAFE_EXT_RE``;
    otherwise it is discarded and derivation falls through to the MIME/kind maps.
    """
    filename = media_obj.get("filename")
    if filename:
        # Strip the leading dot and validate against the safe pattern; a hostile or
        # malformed suffix (backslash, control char, RTL-override, over-long) is
        # rejected here rather than leaking into media.path.
        suffix = PurePosixPath(str(filename)).suffix.lower().lstrip(".")
        if _SAFE_EXT_RE.match(suffix):
            return f".{suffix}"
    mime = media_obj.get("mime")
    if mime and mime in _MIME_EXT:
        return _MIME_EXT[mime]
    return _KIND_EXT.get(str(media_obj.get("kind", "")), ".bin")


# --- path assignment ---------------------------------------------------------


def relative_media_path(
    chat_id: int, message_id: int, ext: str, *, index: int | None = None
) -> str:
    """Build the deterministic, POSIX-relative media path for a message file.

    ``media/<chat_id>/<message_id>.<ext>`` for the usual single-file message; the
    ``_<n>`` suffix is added only when a message carries multiple files (ADR-0003).
    """
    suffix = f"_{index}" if index is not None else ""
    # Always forward-slash separated: the path is a portable relative reference in
    # the contract, not an OS path (ADR-0004 — no run/platform-varying fields).
    return f"media/{int(chat_id)}/{int(message_id)}{suffix}{ext}"


# --- core download -----------------------------------------------------------


async def _download_one(
    client: Any,
    raw: Any,
    media_obj: dict[str, Any],
    *,
    chat_id: int,
    message_id: int,
    output: os.PathLike[str] | str,
    no_media: bool,
    max_media_mb: int | None,
    index: int | None,
) -> None:
    """Resolve one file's ``path``/``skipped`` on ``media_obj`` (mutated in place)."""
    # --no-media: keep the metadata object, download nothing, leave path null, and
    # crucially do NOT set `skipped` (that flag means "oversize", not "opted out").
    if no_media:
        return

    size = int(media_obj.get("size") or 0)
    if max_media_mb is not None and size > max_media_mb * 1024 * 1024:
        # Oversize: honest skip-stub — emit the object with path null + skipped true,
        # download nothing.
        media_obj["skipped"] = True
        log_event("media_skipped", chat=chat_id, msg=message_id, size=size)
        return

    rel = relative_media_path(chat_id, message_id, _extension_for(media_obj), index=index)
    dest = Path(output) / rel

    # Idempotent re-download: an existing file at the expected size is authoritative;
    # set the path and skip the fetch entirely. The check compares against the
    # DECLARED metadata size (as the spec mandates), which assumes the downloaded
    # byte count equals the declared size — true for Telegram. If they ever diverge,
    # the file is re-fetched every run by design (correctness over a stale cache).
    if dest.exists() and dest.stat().st_size == size:
        media_obj["path"] = rel
        log_event("media_cached", chat=chat_id, msg=message_id, path=rel)
        return

    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        await client.download_media(raw, file=str(dest))
    except Exception as exc:  # noqa: BLE001 - re-raised with boundary context (not M6)
        raise ExportError(
            f"chat {chat_id}: message {message_id}: media download failed: {exc}",
            chat=chat_id,
            msg=message_id,
        ) from exc
    media_obj["path"] = rel
    log_event("media_downloaded", chat=chat_id, msg=message_id, path=rel, size=size)


async def download_media_group(
    client: Any,
    raw: Any,
    media_objs: list[dict[str, Any]],
    *,
    chat_id: int,
    message_id: int,
    output: os.PathLike[str] | str,
    no_media: bool = False,
    max_media_mb: int | None = None,
) -> None:
    """Download every file a message carries, assigning ``_<n>`` only when >1.

    A single-file message (the common case) gets ``<message_id>.<ext>``; a message
    carrying multiple files gets ``<message_id>_1.<ext>``, ``_2``, ... (ADR-0003).
    Each object in ``media_objs`` is mutated in place with its ``path``/``skipped``.
    """
    total = len(media_objs)
    for i, media_obj in enumerate(media_objs, start=1):
        index = i if total > 1 else None
        await _download_one(
            client,
            raw,
            media_obj,
            chat_id=chat_id,
            message_id=message_id,
            output=output,
            no_media=no_media,
            max_media_mb=max_media_mb,
            index=index,
        )


async def download_message_media(
    client: Any,
    raw: Any,
    obj: dict[str, Any],
    *,
    chat_id: int,
    output: os.PathLike[str] | str,
    config: Any,
) -> None:
    """Export seam: download the mapped message ``obj``'s media (if any) in place.

    Called from the export walk after mapping and before schema validation, so the
    ``media.path`` an object carries is already set when its NDJSON line is written
    (and thus captured by the golden). A no-op for a message with no downloadable
    ``media`` block.
    """
    media_obj = obj.get("media")
    if media_obj is None:
        return
    await download_media_group(
        client,
        raw,
        [media_obj],
        chat_id=chat_id,
        message_id=int(obj["id"]),
        output=output,
        no_media=config.no_media,
        max_media_mb=config.max_media_mb,
    )


__all__ = [
    "download_media_group",
    "download_message_media",
    "relative_media_path",
]
