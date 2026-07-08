"""Message-mapping fidelity tests (SPEC-0001 REQ "Message Mapping Fidelity").

Every case drives the production :func:`tg_export.mapping.map_message` against the
synthetic Telethon-shaped fixtures — offline, no network. The emphasis is the
fidelity tdl lacked (senders, service events, reactions, replies, forwards) plus
the ADR-0005 invariant: link entities resolve to absolute URLs and no UTF-16
offset ever crosses the boundary.
"""

from __future__ import annotations

import synthetic
from tg_export import mapping, schemas

SELF_ID = synthetic.SELF_ID


def _by_id(chat_id: int, msg_id: int):
    for msg in synthetic.RAW_CHATS[chat_id]["messages"]:
        if msg.id == msg_id:
            return msg
    raise AssertionError(f"no fixture message {msg_id} in chat {chat_id}")


def _map(chat_id: int, msg_id: int) -> dict:
    return mapping.map_message(_by_id(chat_id, msg_id), chat_id=chat_id, self_id=SELF_ID)


def test_every_mapped_message_validates_against_schema():
    for obj in synthetic.all_valid_messages():
        schemas.validate("message", obj)


def test_plain_message_resolves_sender():
    obj = _map(1001, 10)
    assert obj["kind"] == "message"
    assert obj["from"] == {
        "id": 900001,
        "is_self": False,
        "name": "Ada Copeland",
        "username": "adacope",
    }
    assert obj["text"].startswith("Anyone up")
    assert "action" not in obj  # a content message never carries an action


def test_link_entities_resolve_to_urls_without_offsets():
    obj = _map(1001, 11)
    assert obj["entities"] == [
        {"type": "url", "url": "https://example.org/ridge-loop"},
        {"type": "text_link", "url": "https://maps.example.org/pin/482"},
    ]
    # ADR-0005: no offset/length leaks across the boundary.
    for entity in obj["entities"]:
        assert set(entity) == {"type", "url"}


def test_url_entity_slice_is_utf16_correct_after_emoji():
    # The channel post places the URL right after a non-BMP emoji (2 UTF-16 units);
    # a naive character-index slice would corrupt it. The URL must come back exact.
    obj = _map(2002, 30)
    assert obj["entities"] == [{"type": "url", "url": "https://example.org/alerts/17"}]


def test_service_message_maps_to_action():
    obj = _map(1001, 16)
    assert obj["kind"] == "service"
    assert obj["action"] == {"type": "chat_joined", "user_id": 900002}


def test_service_pin_action():
    obj = _map(5005, 21)
    assert obj["kind"] == "service"
    assert obj["action"] == {"type": "pin_message", "message_id": 20}


def test_unresolved_sender_degrades_to_unknown():
    obj = _map(1001, 15)
    assert obj["from"]["name"] == "Unknown"
    assert obj["from"]["id"] is None
    assert obj["from"]["is_self"] is False


def test_reply_and_reactions_present():
    obj = _map(1001, 13)
    assert obj["reply_to_message_id"] == 10
    assert obj["reactions"] == [{"emoji": "❤", "count": 2}]


def test_forward_present():
    obj = _map(1001, 14)
    assert obj["forward"] == {
        "from_name": "Mountain Weather Bot",
        "from_id": 700700,
        "date": 1719788000,
    }


def test_edit_date_carried():
    obj = _map(1001, 17)
    assert obj["edit_date"] == 1719900500


def test_self_sender_marked_is_self():
    obj = _map(5005, 20)
    assert obj["from"]["is_self"] is True
    assert obj["from"]["id"] == SELF_ID


def test_channel_post_has_null_sender_id():
    # Anonymous channel post: display name kept, personal id nulled.
    obj = _map(2002, 31)
    assert obj["from"]["name"] == "Trail Alerts"
    assert obj["from"]["id"] is None


def test_photo_media_metadata_path_null():
    obj = _map(1001, 12)
    assert obj["media"] == {
        "kind": "photo",
        "mime": "image/jpeg",
        "size": 184320,
        "path": None,
        "width": 1280,
        "height": 960,
    }


def test_video_document_classified():
    obj = _map(1001, 15)
    media = obj["media"]
    assert media["kind"] == "video"
    assert media["mime"] == "video/mp4"
    assert media["duration"] == 42.5
    assert media["filename"] == "clip.mp4"
    assert media["path"] is None  # download is M4
    assert "skipped" not in media  # the oversize stub is M4, not M3


def test_plain_document_classified():
    obj = _map(1001, 17)
    assert obj["media"]["kind"] == "document"
    assert obj["media"]["mime"] == "application/pdf"
    assert obj["media"]["path"] is None


def test_media_caption_flattens_into_text():
    # Telethon carries a media caption in msg.message, so text captures it.
    obj = _map(1001, 12)
    assert obj["text"] == "Trailhead this morning"
