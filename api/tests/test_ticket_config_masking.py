"""Ticket server config secret masking (S-L4 review fix).

The workflow-level `ticket_mcp_server` override carries a live org bearer
key. API responses must mask it (any workflow-read credential could
otherwise exfiltrate the org's MCP access), and a save that echoes the
mask back must restore the stored real key instead of persisting the
placeholder.
"""

from api.services.configuration.masking import (
    TICKET_SERVER_CONFIG_KEY,
    mask_key,
    mask_workflow_configurations,
)
from api.services.configuration.merge import merge_ticket_server_secret

REAL_KEY = "dgr-key-1234567890abcdef"


def _configs(api_key=REAL_KEY):
    return {
        TICKET_SERVER_CONFIG_KEY: {
            "enabled": True,
            "url": "http://tickets.internal/mcp",
            "api_key": api_key,
            "timeout_seconds": 3.0,
        },
        "other_key": {"untouched": True},
    }


def test_mask_hides_ticket_server_api_key():
    original = _configs()
    masked = mask_workflow_configurations(original)

    assert masked[TICKET_SERVER_CONFIG_KEY]["api_key"] == mask_key(REAL_KEY)
    assert REAL_KEY not in masked[TICKET_SERVER_CONFIG_KEY]["api_key"]
    # Non-secret fields and unrelated keys pass through.
    assert masked[TICKET_SERVER_CONFIG_KEY]["url"] == "http://tickets.internal/mcp"
    assert masked["other_key"] == {"untouched": True}
    # The stored config is never mutated.
    assert original[TICKET_SERVER_CONFIG_KEY]["api_key"] == REAL_KEY


def test_mask_roundtrip_restores_real_key():
    existing = _configs()
    echoed_back = mask_workflow_configurations(existing)

    merged = merge_ticket_server_secret(echoed_back, existing)

    assert merged[TICKET_SERVER_CONFIG_KEY]["api_key"] == REAL_KEY


def test_new_key_wins_over_existing():
    merged = merge_ticket_server_secret(_configs(api_key="dgr-new-key"), _configs())
    assert merged[TICKET_SERVER_CONFIG_KEY]["api_key"] == "dgr-new-key"


def test_stale_mask_is_left_masked_for_route_rejection():
    """A mask of a rotated/absent key cannot be restored. The merge leaves it
    masked so the update route can 422 instead of persisting the placeholder
    as the live bearer key (which would 401 every handoff write silently)."""
    stale = _configs(api_key=mask_key("dgr-rotated-away-key"))
    merged = merge_ticket_server_secret(stale, _configs())
    assert merged[TICKET_SERVER_CONFIG_KEY]["api_key"] == mask_key(
        "dgr-rotated-away-key"
    )


def test_merge_tolerates_missing_sections():
    assert merge_ticket_server_secret(None, _configs()) is None
    incoming_without_ticket = {"model_overrides": {}}
    assert (
        merge_ticket_server_secret(incoming_without_ticket, _configs())
        == incoming_without_ticket
    )
    incoming = _configs()
    assert merge_ticket_server_secret(incoming, {}) == incoming
