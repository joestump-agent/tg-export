"""Reliability tests — flood-wait survival, best-effort tolerance, resume, JSON logs.

Drives :func:`tg_export.export.export_with_client` against fakes that raise
Telethon's ``FloodWaitError`` and per-message/per-chat errors, proving SPEC-0001
REQ "Reliability and Rate Limits" (M6): a flood-wait is slept and resumed (never
fatal), a single bad message or chat is logged and skipped (never aborts), a killed
run is resumed cleanly from the partial NDJSON, and ``--json-logs`` emits one
machine-ingestible JSON object per line with no message body.

All offline, 100% synthetic; the injected sleeper means no test ever really sleeps.

# Governing: SPEC-0001 REQ "Reliability and Rate Limits", REQ "Error Handling
#            Standards"; ADR-0002, ADR-0008
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from telethon.errors import FloodWaitError

from synthetic import GENERATED_AT, FakeTelegramClient, Msg, User, _dt
from tg_export import export, jsonio, mapping, reliability, schemas
from tg_export import logging as tg_logging
from tg_export.errors import NetworkError, NotAuthorizedError

SELF_ID = 424242
ACCOUNT = {"id": SELF_ID, "username": "trailmix", "phone_last4": "6789"}
DREW = User(900004, "Drew", "Vance", username="drewvance")


def _msg(mid: int) -> Msg:
    return Msg(id=mid, date=_dt(1719800000 + mid), message=f"note {mid}", sender=DREW)


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


def _run(client: Any, config: export.ExportConfig) -> dict[str, Any]:
    return asyncio.run(export.export_with_client(client, config))


def _read_objs(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def record_sleep(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Inject the reliability sleep seam with a recorder so no test really sleeps."""
    slept: list[int] = []

    async def _fake_sleep(seconds: int) -> None:
        slept.append(seconds)

    monkeypatch.setattr(reliability, "_sleep", _fake_sleep)
    return slept


# --- flood-wait: survived, never fatal ---------------------------------------


class FloodDuringIterClient(FakeTelegramClient):
    """Raises ``FloodWaitError`` once, mid-iteration, before a target message id."""

    def __init__(self, chats, account, *, flood_at_id: int, seconds: int) -> None:
        super().__init__(chats, account)
        self._flood_at = flood_at_id
        self._seconds = seconds
        self._flooded = False

    async def iter_messages(self, chat_id: int, *, min_id: int = 0, **_: Any):
        for message in self._chats[chat_id]["messages"]:
            if message.id <= min_id:
                continue
            if not self._flooded and message.id == self._flood_at:
                self._flooded = True
                raise FloodWaitError(request=None, capture=self._seconds)
            yield message


class FloodDuringDownloadClient(FakeTelegramClient):
    """Raises ``FloodWaitError`` on the first ``download_media``, then succeeds."""

    def __init__(self, *args: Any, seconds: int, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._seconds = seconds
        self._flooded = False

    async def download_media(self, message: Any, file: str | None = None) -> str | None:
        if not self._flooded:
            self._flooded = True
            raise FloodWaitError(request=None, capture=self._seconds)
        return await super().download_media(message, file=file)


def test_floodwait_during_iteration_is_survived(tmp_path: Path, record_sleep, caplog):
    caplog.set_level(logging.INFO, logger="tg_export")
    client = FloodDuringIterClient(
        {3003: _chat_meta(3003, [_msg(1), _msg(2), _msg(3)])},
        ACCOUNT,
        flood_at_id=2,
        seconds=7,
    )
    manifest = _run(client, export.ExportConfig(output=tmp_path, generated_at=GENERATED_AT))

    # The export completed WITHOUT failing and every message was written exactly
    # once (iteration resumed from the last written id — no dupes, no gaps).
    ids = [o["id"] for o in _read_objs(tmp_path / "chats" / "3003.ndjson")]
    assert ids == [1, 2, 3]
    assert manifest["chats"][0]["message_count"] == 3
    # Slept exactly the requested duration, exactly once.
    assert record_sleep == [7]
    # The wait is logged in seconds at INFO — seconds only, no body.
    assert "event=flood_wait" in caplog.text
    assert "seconds=7" in caplog.text
    assert "note 2" not in caplog.text


def test_floodwait_during_download_is_survived(tmp_path: Path, record_sleep, caplog):
    caplog.set_level(logging.INFO, logger="tg_export")
    client = FloodDuringDownloadClient(seconds=12)  # default synthetic fixtures
    _run(client, export.ExportConfig(output=tmp_path, generated_at=GENERATED_AT))

    # The photo (msg 12) still downloaded after the flood-wait was slept + retried.
    objs = _read_objs(tmp_path / "chats" / "1001.ndjson")
    photo = next(o for o in objs if o["id"] == 12)
    assert photo["media"]["path"] == "media/1001/12.jpg"
    assert (tmp_path / "media" / "1001" / "12.jpg").is_file()
    assert record_sleep == [12]
    assert "event=flood_wait" in caplog.text and "seconds=12" in caplog.text
    # A flood-wait is survived, NOT treated as a per-message failure.
    assert "event=message_skipped" not in caplog.text


# --- best-effort: one bad chat does not abort the run ------------------------


class ChatAccessErrorClient(FakeTelegramClient):
    """Raises a non-flood error when a specific chat is iterated (inaccessible peer)."""

    def __init__(self, chats, account, *, bad_chat_id: int) -> None:
        super().__init__(chats, account)
        self._bad = bad_chat_id

    async def iter_messages(self, chat_id: int, *, min_id: int = 0, **kw: Any):
        if chat_id == self._bad:
            raise RuntimeError("CHANNEL_PRIVATE")
        async for message in super().iter_messages(chat_id, min_id=min_id, **kw):
            yield message


def test_bad_chat_is_skipped_not_fatal(tmp_path: Path, caplog):
    caplog.set_level(logging.WARNING, logger="tg_export")
    client = ChatAccessErrorClient(
        {
            3003: _chat_meta(3003, [_msg(1), _msg(2)]),
            4004: _chat_meta(4004, [_msg(1)]),
        },
        ACCOUNT,
        bad_chat_id=4004,
    )
    manifest = _run(client, export.ExportConfig(output=tmp_path, generated_at=GENERATED_AT))

    # The good chat exported; the inaccessible chat is skipped, not in the manifest;
    # the run still completed with a valid manifest.
    assert [c["id"] for c in manifest["chats"]] == [3003]
    assert (tmp_path / "chats" / "3003.ndjson").exists()
    schemas.validate("manifest", manifest)
    for obj in _read_objs(tmp_path / "chats" / "3003.ndjson"):
        schemas.validate("message", obj)
    # The skip is logged with chat context and its cause — never silently swallowed.
    assert "event=chat_error_skipped" in caplog.text
    assert "chat=4004" in caplog.text
    assert "cause=RuntimeError" in caplog.text


class ChatFatalErrorClient(FakeTelegramClient):
    """Raises a *genuine* (non-best-effort) error when a specific chat is iterated."""

    def __init__(self, chats, account, *, bad_chat_id: int, error: Exception) -> None:
        super().__init__(chats, account)
        self._bad = bad_chat_id
        self._error = error

    async def iter_messages(self, chat_id: int, *, min_id: int = 0, **kw: Any):
        if chat_id == self._bad:
            raise self._error
        async for message in super().iter_messages(chat_id, min_id=min_id, **kw):
            yield message


@pytest.mark.parametrize(
    "error",
    [NotAuthorizedError("tg-export: not authorized"), NetworkError("connection reset")],
)
def test_genuine_auth_or_network_error_mid_walk_is_not_swallowed(tmp_path: Path, error):
    """Best-effort tolerance MUST NOT swallow a real auth/network failure.

    A NotAuthorized/Network error raised while iterating a chat has to propagate out
    of the walk (surfacing the dedicated exit code), NOT be caught as a best-effort
    "skipped chat" — otherwise an expired session mid-run would silently exit 0 with a
    truncated archive. Locks in the re-raise guard in ``export_with_client``
    (SPEC-0001 REQ "Reliability", "Error Handling Standards").
    """
    client = ChatFatalErrorClient(
        {3003: _chat_meta(3003, [_msg(1), _msg(2)])},
        ACCOUNT,
        bad_chat_id=3003,
        error=error,
    )
    with pytest.raises(type(error)):
        _run(client, export.ExportConfig(output=tmp_path, generated_at=GENERATED_AT))


# --- killed-run resume: valid partial tree, --since continues cleanly --------


def test_killed_run_resumes_from_partial_ndjson(tmp_path: Path, caplog):
    caplog.set_level(logging.INFO, logger="tg_export")
    tree = tmp_path / "tree"

    # 1. A complete first run writes [1,2,3] + a manifest anchored at 3.
    first = FakeTelegramClient({3003: _chat_meta(3003, [_msg(1), _msg(2), _msg(3)])}, ACCOUNT)
    _run(first, export.ExportConfig(output=tree, generated_at=GENERATED_AT))

    # 2. Simulate a killed incremental run: two more lines were appended (per-line
    #    flush leaves complete lines) but the manifest — written LAST — was never
    #    rewritten. Delete it to model the kill.
    ndjson = tree / "chats" / "3003.ndjson"
    appended = [mapping.map_message(m, chat_id=3003, self_id=SELF_ID) for m in (_msg(4), _msg(5))]
    with ndjson.open("a", encoding="utf-8") as fh:
        for obj in appended:
            fh.write(jsonio.ndjson_line(obj))
    (tree / "manifest.json").unlink()

    # 3. Resume with --since against the partial tree: anchors are recomputed from
    #    the NDJSON (max id 5), so only id 6 is new.
    resume = FakeTelegramClient(
        {3003: _chat_meta(3003, [_msg(i) for i in range(1, 7)])}, ACCOUNT
    )
    manifest = _run(resume, export.ExportConfig(output=tree, since=tree, generated_at=GENERATED_AT))

    objs = _read_objs(ndjson)
    assert [o["id"] for o in objs] == [1, 2, 3, 4, 5, 6]  # no dupes, no gaps
    entry = manifest["chats"][0]
    assert entry["message_count"] == 6
    assert entry["max_message_id"] == 6
    schemas.validate("manifest", manifest)
    for obj in objs:
        schemas.validate("message", obj)
    # The resume-from-partial path was taken (manifest was absent).
    assert "event=resume_from_partial" in caplog.text


def test_since_with_no_manifest_and_no_ndjson_is_malformed(tmp_path: Path):
    # A --since dir with neither a manifest nor any NDJSON has nothing to resume from
    # and stays a stable malformed-argument error (not a silent full export).
    from tg_export.errors import MalformedArgumentError

    client = FakeTelegramClient({3003: _chat_meta(3003, [_msg(1)])}, ACCOUNT)
    with pytest.raises(MalformedArgumentError) as exc:
        _run(client, export.ExportConfig(output=tmp_path / "out", since=tmp_path / "empty"))
    assert "--since" in str(exc.value)


# --- --json-logs: one JSON object per line, never a body ---------------------


def test_json_logs_render_is_parseable_and_structured():
    stream = io.StringIO()
    tg_logging.configure(json_logs=True, stream=stream)
    tg_logging.log_event("chat_exported", chat=1001, messages=8, total=8, skipped=0)
    obj = json.loads(stream.getvalue().strip())  # parses as one JSON object
    assert obj["event"] == "chat_exported"
    assert obj["chat"] == 1001 and obj["skipped"] == 0


def test_default_logs_are_key_value_not_json():
    stream = io.StringIO()
    tg_logging.configure(json_logs=False, stream=stream)
    tg_logging.log_event("chat_exported", chat=1001)
    line = stream.getvalue().strip()
    assert line == "event=chat_exported chat=1001"


def test_json_logs_full_export_lines_parse_and_have_no_bodies(tmp_path: Path):
    stream = io.StringIO()
    tg_logging.configure(json_logs=True, stream=stream)
    client = FakeTelegramClient()  # the full synthetic fixtures (incl. media + channel)
    _run(client, export.ExportConfig(output=tmp_path, generated_at=GENERATED_AT))

    raw = stream.getvalue()
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    assert lines  # progress was actually emitted
    for line in lines:
        obj = json.loads(line)  # EVERY line is valid JSON
        assert "event" in obj
    # No message body ever appears in the machine logs.
    for body in (
        "Anyone up for the ridge loop",
        "Remember to charge the GPS",
        "Trailhead road closed",
    ):
        assert body not in raw
