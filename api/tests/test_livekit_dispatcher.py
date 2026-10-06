"""Unit tests for the LiveKit dispatcher DID parsing and dedup (S-L1-DISPATCH)."""

import types

from api.services.pipecat.livekit_dispatcher import (
    DISPATCH_DEDUP_TTL_SECONDS,
    DispatchDedup,
    did_from_sip_attributes,
    dispatch_key,
)


def test_did_from_sip_attribute_e164():
    assert (
        did_from_sip_attributes({"sip.trunkPhoneNumber": "+886212345678"})
        == "+886212345678"
    )


def test_did_from_sip_attribute_national_format_uses_tw_hint():
    # task 0.3: carriers may send the dialed number as "0212345678".
    assert (
        did_from_sip_attributes({"sip.trunkPhoneNumber": "0212345678"})
        == "+886212345678"
    )


def test_did_missing_attribute():
    assert did_from_sip_attributes({"sip.phoneNumber": "+886911000001"}) is None
    assert did_from_sip_attributes({}) is None


def test_did_not_phone_shaped():
    assert did_from_sip_attributes({"sip.trunkPhoneNumber": "alice"}) is None
    assert did_from_sip_attributes({"sip.trunkPhoneNumber": "sip:a@b"}) is None
    assert did_from_sip_attributes({"sip.trunkPhoneNumber": "   "}) is None


def test_room_name_is_never_a_did_source():
    # The callee rule names rooms cs-_<dialed>_<random>; DID comes only from
    # the attribute.
    assert did_from_sip_attributes({}) is None


def test_dispatch_key_prefers_sip_call_id():
    p = types.SimpleNamespace(sid="PA_1", attributes={"sip.callID": "SCL_abc"})
    assert dispatch_key(p) == "SCL_abc"
    assert dispatch_key(types.SimpleNamespace(sid="PA_1", attributes={})) == "PA_1"


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_dedup_claim_commit_blocks_replay():
    d = DispatchDedup()
    assert d.claim("SCL_1")
    assert not d.claim("SCL_1")  # in flight: concurrent redelivery dropped
    d.commit("SCL_1")
    assert not d.claim("SCL_1")  # committed: replay dropped


def test_dedup_release_allows_next_attempt():
    d = DispatchDedup()
    assert d.claim("SCL_1")
    d.release("SCL_1")
    assert d.claim("SCL_1")


def test_dedup_entries_expire_after_ttl():
    clock = _Clock()
    d = DispatchDedup(clock=clock)
    d.claim("SCL_1")
    d.commit("SCL_1")
    clock.t += DISPATCH_DEDUP_TTL_SECONDS + 1
    assert not d.is_known("SCL_1")
    assert d.claim("SCL_1")


def test_dedup_ttl_exceeds_webhook_token_lifetime():
    # webhook JWT exp = issue + 300 s (task 0.3, security L6)
    assert DISPATCH_DEDUP_TTL_SECONDS > 300
