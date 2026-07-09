"""Media download tests — offline, mocked Telethon, 100% synthetic.

Covers SPEC-0001 REQ "Media Handling": a photo downloads to a relative path and
``media.path`` matches (scenario 1); an oversize file degrades to an honest
``path: null, skipped: true`` skip-stub with no download (scenario 2); an existing
file at the expected size is not re-fetched (scenario 3); ``--no-media`` downloads
nothing while keeping the metadata object; a multi-file message gets ``_1``/``_2``
suffixes; and media paths + bytes are deterministic (ADR-0004). A download error is
wrapped with layer-boundary context and, in the export walk, downgraded to a logged
best-effort skip (M6) — never silently swallowed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import pytest

import synthetic
from synthetic import FakeTelegramClient
from tg_export import export, mapping, media, schemas
from tg_export.errors import ExportError


def _run(client: Any, config: export.ExportConfig) -> dict:
    return asyncio.run(export.export_with_client(client, config))


def _read_ndjson(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _media_by_id(objs: list[dict], msg_id: int) -> dict:
    for obj in objs:
        if obj["id"] == msg_id:
            return obj["media"]
    raise AssertionError(f"no message {msg_id}")


def _one_chat_client(message: synthetic.Msg, *, chat_id: int = 7007) -> FakeTelegramClient:
    """A fake client with a single private chat holding one message."""
    meta = {
        "type": "private",
        "title": "Solo Chat",
        "username": None,
        "entity_id": chat_id,
        "megagroup": False,
        "broadcast": False,
        "is_user": True,
        "messages": [message],
    }
    return FakeTelegramClient(chats={chat_id: meta}, account=synthetic.ACCOUNT)


# --- scenario 1: media downloads to a relative path --------------------------


def test_photo_downloads_to_relative_path(tmp_path: Path):
    client = FakeTelegramClient()
    _run(client, export.ExportConfig(output=tmp_path, generated_at=synthetic.GENERATED_AT))

    objs = _read_ndjson(tmp_path / "chats" / "1001.ndjson")
    photo = _media_by_id(objs, 12)
    assert photo["path"] == "media/1001/12.jpg"
    assert "skipped" not in photo

    downloaded = tmp_path / "media" / "1001" / "12.jpg"
    assert downloaded.is_file()
    assert downloaded.stat().st_size == photo["size"] == 3072
    # The path is relative — no absolute path leaks into the contract (ADR-0004).
    assert not Path(photo["path"]).is_absolute()


def test_document_and_video_download_with_derived_extensions(tmp_path: Path):
    client = FakeTelegramClient()
    _run(client, export.ExportConfig(output=tmp_path))
    objs = _read_ndjson(tmp_path / "chats" / "1001.ndjson")
    # Extension comes from the original filename when present.
    assert _media_by_id(objs, 15)["path"] == "media/1001/15.mp4"
    assert _media_by_id(objs, 17)["path"] == "media/1001/17.pdf"
    assert (tmp_path / "media" / "1001" / "15.mp4").is_file()
    assert (tmp_path / "media" / "1001" / "17.pdf").is_file()
    # Three downloadable files in chat 1001 (photo + video + pdf).
    assert client.download_calls == 3


# --- scenario 2: oversize degrades to a skip-stub ----------------------------


def test_oversize_media_is_skip_stub_no_download(tmp_path: Path):
    big = synthetic.Msg(
        id=50,
        date=synthetic._dt(1719800000),
        message="huge attachment",
        sender=synthetic.ADA,
        media=synthetic.MessageMediaDocument(
            synthetic.Document(
                "application/zip", 5 * 1024 * 1024, [synthetic.DocumentAttributeFilename("big.zip")]
            )
        ),
    )
    client = _one_chat_client(big)
    _run(client, export.ExportConfig(output=tmp_path, max_media_mb=1))

    obj = _read_ndjson(tmp_path / "chats" / "7007.ndjson")[0]
    assert obj["media"]["path"] is None
    assert obj["media"]["skipped"] is True
    assert obj["media"]["size"] == 5 * 1024 * 1024
    # Nothing downloaded, no file written.
    assert client.download_calls == 0
    assert not (tmp_path / "media").exists()


def test_under_limit_media_still_downloads(tmp_path: Path):
    # A file at exactly the limit boundary (not over) downloads normally.
    client = FakeTelegramClient()
    _run(client, export.ExportConfig(output=tmp_path, max_media_mb=1))
    objs = _read_ndjson(tmp_path / "chats" / "1001.ndjson")
    assert _media_by_id(objs, 12)["path"] == "media/1001/12.jpg"  # 3 KiB < 1 MiB
    assert client.download_calls == 3


# --- scenario 3: existing file at expected size is not re-downloaded ----------


def test_idempotent_redownload_skips_existing(tmp_path: Path):
    first = FakeTelegramClient()
    _run(first, export.ExportConfig(output=tmp_path))
    assert first.download_calls == 3  # fresh dir: all three fetched

    # A second run into the SAME dir must not re-fetch any already-present file.
    second = FakeTelegramClient()
    _run(second, export.ExportConfig(output=tmp_path))
    assert second.download_calls == 0

    # Paths are still set on the re-run's emitted objects.
    objs = _read_ndjson(tmp_path / "chats" / "1001.ndjson")
    assert _media_by_id(objs, 12)["path"] == "media/1001/12.jpg"


def test_wrong_size_existing_file_is_refetched(tmp_path: Path):
    # A partially-written file (size mismatch) is NOT treated as cached.
    stale = tmp_path / "media" / "1001" / "12.jpg"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"partial")  # 7 bytes != expected 3072

    client = FakeTelegramClient()
    _run(client, export.ExportConfig(output=tmp_path))
    assert client.download_calls == 3
    assert stale.stat().st_size == 3072  # re-fetched to the full expected size


# --- --no-media: metadata only, no download ----------------------------------


def test_no_media_keeps_metadata_but_downloads_nothing(tmp_path: Path):
    client = FakeTelegramClient()
    _run(client, export.ExportConfig(output=tmp_path, no_media=True))

    objs = _read_ndjson(tmp_path / "chats" / "1001.ndjson")
    photo = _media_by_id(objs, 12)
    assert photo["kind"] == "photo"  # metadata object still emitted
    assert photo["path"] is None
    assert "skipped" not in photo  # opting out is NOT a skip
    assert client.download_calls == 0
    assert not (tmp_path / "media").exists()


# --- multi-file message: _1 / _2 suffixes ------------------------------------


def test_multi_file_message_gets_indexed_suffixes(tmp_path: Path):
    raw = synthetic.Msg(
        id=60,
        date=synthetic._dt(1719800000),
        sender=synthetic.ADA,
        media=synthetic.MessageMediaPhoto(synthetic.Photo([synthetic.PhotoSize(640, 480, 3072)])),
    )
    first = mapping.map_media(raw.media)
    second = {"kind": "document", "mime": "application/pdf", "size": 4096, "path": None,
              "filename": "notes.pdf"}
    client = FakeTelegramClient()

    asyncio.run(
        media.download_media_group(
            client, raw, [first, second], chat_id=9009, message_id=60, output=tmp_path
        )
    )

    assert first["path"] == "media/9009/60_1.jpg"
    assert second["path"] == "media/9009/60_2.pdf"
    assert (tmp_path / "media" / "9009" / "60_1.jpg").is_file()
    assert (tmp_path / "media" / "9009" / "60_2.pdf").is_file()


def test_single_file_message_has_no_suffix(tmp_path: Path):
    raw = synthetic.Msg(
        id=61,
        date=synthetic._dt(1719800000),
        sender=synthetic.ADA,
        media=synthetic.MessageMediaPhoto(synthetic.Photo([synthetic.PhotoSize(640, 480, 3072)])),
    )
    only = mapping.map_media(raw.media)
    asyncio.run(
        media.download_media_group(
            client=FakeTelegramClient(), raw=raw, media_objs=[only],
            chat_id=9009, message_id=61, output=tmp_path,
        )
    )
    assert only["path"] == "media/9009/61.jpg"  # no _<n> for a single file


# --- determinism: ADR-0004 ---------------------------------------------------


def test_media_paths_and_bytes_are_deterministic(tmp_path: Path):
    one = tmp_path / "run1"
    two = tmp_path / "run2"
    _run(FakeTelegramClient(), export.ExportConfig(output=one, generated_at=synthetic.GENERATED_AT))
    _run(FakeTelegramClient(), export.ExportConfig(output=two, generated_at=synthetic.GENERATED_AT))

    for rel in ("media/1001/12.jpg", "media/1001/15.mp4", "media/1001/17.pdf"):
        assert (one / rel).read_bytes() == (two / rel).read_bytes()
    # And the emitted relative paths are identical.
    assert (one / "chats" / "1001.ndjson").read_bytes() == (
        two / "chats" / "1001.ndjson"
    ).read_bytes()


# --- error handling: wrapped with context, downgraded to a best-effort skip ---


def test_download_error_wrapped_with_context_direct_call(tmp_path: Path):
    # A direct media-download call (outside the walk's tolerance) still wraps the
    # failure with layer-boundary context and never swallows it.
    class BoomClient(FakeTelegramClient):
        async def download_media(self, message: Any, file: str | None = None):
            raise RuntimeError("connection reset")

    raw = synthetic.CHAT_1001_MESSAGES[2]  # the photo message (id 12)
    obj = mapping.map_message(raw, chat_id=1001, self_id=synthetic.SELF_ID)
    with pytest.raises(ExportError) as exc:
        asyncio.run(
            media.download_message_media(
                BoomClient(), raw, obj, chat_id=1001, output=tmp_path,
                config=export.ExportConfig(output=tmp_path),
            )
        )
    message = str(exc.value)
    assert "chat 1001:" in message
    assert "message 12:" in message
    assert "media download failed: connection reset" in message


def test_download_error_in_walk_is_skipped_not_fatal(tmp_path: Path, caplog):
    # M6 best-effort tolerance: an undownloadable file is logged and its message
    # skipped; the rest of the chat still exports and the run completes.
    class BoomClient(FakeTelegramClient):
        async def download_media(self, message: Any, file: str | None = None):
            raise RuntimeError("connection reset")

    caplog.set_level(logging.WARNING, logger="tg_export")
    manifest = _run(BoomClient(), export.ExportConfig(output=tmp_path))

    objs = _read_ndjson(tmp_path / "chats" / "1001.ndjson")
    ids = [o["id"] for o in objs]
    # The three media messages (12, 15, 17) were skipped; text-only ones survived.
    assert 12 not in ids and 15 not in ids and 17 not in ids
    assert 10 in ids and 11 in ids
    for obj in objs:
        assert obj.get("media") is None  # only non-media messages remain
    schemas.validate("manifest", manifest)
    assert "event=message_skipped" in caplog.text
    assert "chat=1001" in caplog.text and "msg=12" in caplog.text


# --- extension derivation unit coverage --------------------------------------


@pytest.mark.parametrize(
    "media_obj, expected",
    [
        ({"kind": "photo", "mime": "image/jpeg"}, ".jpg"),
        ({"kind": "video", "mime": "video/mp4", "filename": "clip.MP4"}, ".mp4"),
        ({"kind": "document", "mime": "application/pdf"}, ".pdf"),
        ({"kind": "document", "mime": "application/x-unknown"}, ".bin"),
        ({"kind": "sticker", "mime": "application/x-unknown"}, ".webp"),
        # A multi-dot filename resolves to the (clean) last suffix.
        ({"kind": "document", "mime": "application/x-unknown", "filename": "backup.tar.gz"}, ".gz"),
    ],
)
def test_extension_derivation(media_obj, expected):
    assert media._extension_for(media_obj) == expected


@pytest.mark.parametrize(
    "hostile_filename",
    [
        "x.ba\\ck",  # backslash injection
        "clip.m\tp4",  # embedded tab / control char
        "f." + "a" * 300,  # absurdly long suffix
        "doc.‮mp4",  # RTL-override unicode
        "weird.a b",  # embedded space
        "shell.$(rm)",  # shell metacharacters
    ],
)
def test_hostile_filename_extension_is_sanitized(hostile_filename):
    # An untrusted/malformed filename suffix must NOT leak into the extension; it
    # degrades to the MIME map (here application/pdf -> .pdf), never a dirty ext.
    media_obj = {"kind": "document", "mime": "application/pdf", "filename": hostile_filename}
    ext = media._extension_for(media_obj)
    assert ext == ".pdf"  # fell through to the clean MIME fallback
    assert "\\" not in ext
    assert all(ch.isprintable() and not ch.isspace() for ch in ext.lstrip("."))


def test_hostile_filename_keeps_media_path_clean(tmp_path: Path):
    # End-to-end: a message whose media filename is hostile still yields a portable,
    # control-char-free media.path.
    raw = synthetic.Msg(
        id=70,
        date=synthetic._dt(1719800000),
        sender=synthetic.ADA,
        media=synthetic.MessageMediaDocument(
            synthetic.Document(
                # Hostile suffix (backslash + tab) -> rejected -> MIME fallback .pdf.
                "application/pdf", 4096, [synthetic.DocumentAttributeFilename("evil.p\\d\tf")]
            )
        ),
    )
    only = mapping.map_media(raw.media)
    asyncio.run(
        media.download_media_group(
            client=FakeTelegramClient(), raw=raw, media_objs=[only],
            chat_id=9009, message_id=70, output=tmp_path,
        )
    )
    assert only["path"] == "media/9009/70.pdf"  # degraded to the clean MIME fallback
    assert "\\" not in only["path"]
    assert all(ch.isprintable() for ch in only["path"])
    assert (tmp_path / "media" / "9009" / "70.pdf").is_file()
