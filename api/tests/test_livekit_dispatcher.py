"""Unit tests for the LiveKit dispatcher DID parsing (S-L1-DISPATCH)."""

from api.services.pipecat.livekit_dispatcher import parse_did_from_room


def test_parse_did_from_room_pstn():
    assert parse_did_from_room("cs-+886912345678") == "+886912345678"


def test_parse_did_from_room_bad_prefix():
    assert parse_did_from_room("other-room") is None


def test_parse_did_from_room_empty_did():
    assert parse_did_from_room("cs-") is None


def test_parse_did_from_room_invalid_did():
    assert parse_did_from_room("cs-   ") is None
