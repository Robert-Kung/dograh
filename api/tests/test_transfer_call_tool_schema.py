"""Transfer-call tool schema carries the transfer-gate keys (fork PR #11).

The in-call gate (press0_gate / execute_cold_transfer / queue_is_healthy) reads
schedule- and queue-health keys from the persisted tool config. Persistence goes
through ``request.definition.model_dump()`` (tool_management.create_tool_for_user),
so any key missing from ``TransferCallConfig`` is silently dropped on write while
the API returns 200 — the gate then runs unconfigured. These tests pin the
round-trip for every key the gate reads.
"""

import pytest
from pydantic import ValidationError

from api.schemas.tool import CreateToolRequest, TransferCallConfig
from api.tests.support.platform_artifacts import requires_sip_uri

GATE_CONFIG = {
    # W2a D-A6: the ARI dialect ("sip/queue@…", "PJSIP/1234", bare E.164)
    # no longer validates. It never dialled on LiveKit; accepting it only
    # produced destinations that failed at REFER time.
    "destination": "sip:queue@example.internal",
    "schedule": {
        "timezone": "Asia/Taipei",
        "weekly": {"mon": [["09:00", "18:00"]]},
    },
    "afterHoursAction": "back_to_ai",
    "afterHoursMessage": "目前非營業時間",
    "alternateDestination": "tel:+886223456789",
    "transferFailedMessage": "轉接失敗",
    "transferUnavailableMessage": "目前無法轉接",
    "unavailableAnnounceLimit": 2,
    "queueHealthUrl": "http://queue:8080/health",
    "queueHealthToken": "token-value",
    "queueHealthTimeoutSeconds": 2.5,
    "queueHealthCacheTtlSeconds": 5.0,
}


def _create_request(config: dict) -> CreateToolRequest:
    return CreateToolRequest.model_validate(
        {
            "name": "transfer to human queue",
            "definition": {
                "schema_version": 1,
                "type": "transfer_call",
                "config": config,
            },
        }
    )


@requires_sip_uri
def test_gate_keys_survive_the_persistence_dump():
    """The exact failure shape this PR fixes: write-path silently dropping keys."""
    request = _create_request(GATE_CONFIG)
    dumped = request.definition.model_dump()["config"]
    for key, want in GATE_CONFIG.items():
        assert dumped[key] == want, f"gate key dropped on model_dump: {key}"


@requires_sip_uri
def test_config_without_gate_keys_still_validates():
    """Pre-existing tools (destination-only) keep working unchanged."""
    request = _create_request({"destination": "tel:+886223456789"})
    dumped = request.definition.model_dump()["config"]
    assert dumped["destination"] == "tel:+886223456789"
    # unset gate keys dump as None, which every reader treats as unconfigured
    assert dumped["queueHealthUrl"] is None
    assert dumped["schedule"] is None


@requires_sip_uri
def test_alternate_destination_validated_like_destination():
    bad = dict(GATE_CONFIG, alternateDestination="not-a-destination")
    # ``match`` pins the *reason* (review M-7). Without the shared parser the
    # write path refuses every destination, so a bare ``raises`` would pass
    # while proving nothing about ``alternateDestination`` at all.
    with pytest.raises(ValidationError, match="Invalid transfer destination"):
        _create_request(bad)


@requires_sip_uri
def test_alternate_destination_accepts_sip_and_none():
    assert (
        TransferCallConfig.model_validate(
            {
                "destination": "tel:+886223456789",
                "alternateDestination": "sip:human@pbx.example",
            }
        ).alternateDestination
        == "sip:human@pbx.example"
    )
    assert (
        TransferCallConfig.model_validate(
            {"destination": "tel:+886223456789"}
        ).alternateDestination
        is None
    )


@requires_sip_uri
def test_unavailable_announce_limit_must_be_positive():
    # Same reason-pinning as above: ``GATE_CONFIG`` carries a destination, so
    # an unmounted parser fails this request before the limit is ever looked at.
    with pytest.raises(ValidationError, match="unavailableAnnounceLimit"):
        _create_request(dict(GATE_CONFIG, unavailableAnnounceLimit=0))
