"""ccp W4c: SIP participant identities in log messages keep only 4 digits."""

import pytest
from loguru import logger

from api.logging_config import mask_sip_numbers


@pytest.fixture
def masked_logs():
    lines: list[str] = []
    logger.configure(patcher=mask_sip_numbers)
    sink = logger.add(lambda m: lines.append(str(m)), format="{message}")
    yield lines
    logger.remove(sink)
    logger.configure(patcher=None)


def test_participant_connected_line_is_masked(masked_logs):
    logger.info("Participant connected: sip_+886912345678")
    assert masked_logs == ["Participant connected: sip_***5678\n"]


@pytest.mark.parametrize(
    "message,expected",
    [
        ("sip_0912345678 left", "sip_***5678 left"),
        ("{'sip.phoneNumber': '+886912345678'}", "{'sip.phoneNumber': '***5678'}"),
        ('"sip.phoneNumber":"0912345678"', '"sip.phoneNumber":"***5678"'),
        ("room cs-_+886212345678_abc", "room cs-_+886212345678_abc"),  # DID, not caller
        ("sip_agent joined", "sip_agent joined"),
    ],
)
def test_patterns(message, expected):
    record = {"message": message}
    mask_sip_numbers(record)
    assert record["message"] == expected
