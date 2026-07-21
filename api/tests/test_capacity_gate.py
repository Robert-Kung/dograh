"""Capacity-gate tests (S-L9-SCALE): limit/config semantics, slot lifecycle,
dispatch admission, overflow action chain, capacity events."""

import asyncio
import types
from datetime import datetime, timezone

import pytest

from api.services.pipecat import active_calls, capacity_gate
from api.services.pipecat.capacity_gate import (
    capacity_overflow,
    validate_capacity_config,
)


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    for var in (
        "LIVEKIT_MAX_CONCURRENT_CALLS",
        "CAPACITY_OVERFLOW_TRANSFER_TO",
        "CAPACITY_OVERFLOW_MAX_INFLIGHT",
        "SAFETYNET_FALLBACK_QUEUE",
    ):
        monkeypatch.delenv(var, raising=False)
    active_calls._active_run_ids.clear()
    active_calls._livekit_run_ids.clear()
    active_calls._reserved_slots = 0
    capacity_gate._overflow_in_progress.clear()
    yield
    active_calls._active_run_ids.clear()
    active_calls._livekit_run_ids.clear()
    active_calls._reserved_slots = 0
    capacity_gate._overflow_in_progress.clear()


@pytest.fixture
def events(monkeypatch):
    captured = []

    def fake_emit(event, **fields):
        captured.append({"event": event, **fields})

    monkeypatch.setattr(capacity_gate.call_events, "emit", fake_emit)
    return captured


@pytest.fixture
def gate_open(monkeypatch):
    async def allow(workflow_id, user_id, now):
        return True

    monkeypatch.setattr(capacity_gate, "_gate_allows", allow)


def _fake_lk(participants=None, *, raise_on_transfer=False, on_list=None):
    captured = {"deleted": [], "transfers": [], "identities": []}

    async def list_participants(req):
        if on_list is not None:
            return await on_list(req)
        return types.SimpleNamespace(participants=participants or [])

    async def transfer(req):
        if raise_on_transfer:
            raise RuntimeError("provider rejected")
        captured["transfers"].append(req.transfer_to)
        captured["identities"].append(req.participant_identity)

    async def delete_room(req):
        captured["deleted"].append(req.room)

    return types.SimpleNamespace(
        room=types.SimpleNamespace(
            list_participants=list_participants, delete_room=delete_room
        ),
        sip=types.SimpleNamespace(transfer_sip_participant=transfer),
    ), captured


def _sip_caller():
    from api.services.pipecat.livekit_cold_transfer import SIP_KIND

    return types.SimpleNamespace(kind=SIP_KIND, identity="sip_abc")


async def _drain():
    for _ in range(10):
        await asyncio.sleep(0)


def _overflow(room, lk, *, active=6, limit=6):
    return capacity_overflow(
        room, active=active, limit=limit, workflow_id=1, user_id=2, lk=lk
    )


# --- config semantics (1.1) ---


def test_default_limit_is_six():
    assert capacity_gate.max_concurrent_calls() == 6


def test_explicit_zero_disables_and_validates(monkeypatch):
    monkeypatch.setenv("LIVEKIT_MAX_CONCURRENT_CALLS", "0")
    validate_capacity_config()
    assert capacity_gate.max_concurrent_calls() == 0


def test_negative_limit_fails_fast(monkeypatch):
    monkeypatch.setenv("LIVEKIT_MAX_CONCURRENT_CALLS", "-1")
    with pytest.raises(RuntimeError, match="LIVEKIT_MAX_CONCURRENT_CALLS"):
        validate_capacity_config()


def test_non_integer_limit_fails_fast(monkeypatch):
    monkeypatch.setenv("LIVEKIT_MAX_CONCURRENT_CALLS", "six")
    with pytest.raises(RuntimeError, match="LIVEKIT_MAX_CONCURRENT_CALLS"):
        validate_capacity_config()


def test_bad_inflight_cap_fails_fast(monkeypatch):
    monkeypatch.setenv("CAPACITY_OVERFLOW_MAX_INFLIGHT", "0")
    with pytest.raises(RuntimeError, match="CAPACITY_OVERFLOW_MAX_INFLIGHT"):
        validate_capacity_config()
    monkeypatch.setenv("CAPACITY_OVERFLOW_MAX_INFLIGHT", "many")
    with pytest.raises(RuntimeError, match="CAPACITY_OVERFLOW_MAX_INFLIGHT"):
        validate_capacity_config()


def test_malformed_overflow_target_fails_fast(monkeypatch):
    monkeypatch.setenv("CAPACITY_OVERFLOW_TRANSFER_TO", "0912345678")
    with pytest.raises(RuntimeError, match="CAPACITY_OVERFLOW_TRANSFER_TO"):
        validate_capacity_config()


def test_premium_rate_target_fails_fast(monkeypatch):
    monkeypatch.setenv("CAPACITY_OVERFLOW_TRANSFER_TO", "tel:+19005550000")
    with pytest.raises(RuntimeError, match="premium-rate"):
        validate_capacity_config()


def test_premium_rate_sip_user_fails_fast(monkeypatch):
    # a '+' in a sip user part is already outside the destination shape, so
    # this is caught by format validation — boot must refuse either way; the
    # premium guard proper covers the tel:+E164 form
    monkeypatch.setenv("CAPACITY_OVERFLOW_TRANSFER_TO", "sip:+8862045555@pbx.example")
    with pytest.raises(RuntimeError, match="CAPACITY_OVERFLOW_TRANSFER_TO"):
        validate_capacity_config()


def test_premium_rate_effective_fallback_fails_fast(monkeypatch):
    # The safetynet queue is what overflow will actually dial when the
    # dedicated var is unset — same blast radius, same guard.
    monkeypatch.setenv("SAFETYNET_FALLBACK_QUEUE", "tel:+19765550000")
    with pytest.raises(RuntimeError, match="premium-rate"):
        validate_capacity_config()


def test_target_falls_back_to_safetynet_queue(monkeypatch):
    monkeypatch.setenv("SAFETYNET_FALLBACK_QUEUE", "tel:+886900000000")
    assert capacity_gate.overflow_transfer_to() == "tel:+886900000000"
    validate_capacity_config()


def test_no_targets_is_valid_but_warned():
    assert capacity_gate.overflow_transfer_to() is None
    validate_capacity_config()  # warning only — the delete-room leg handles it


# --- slot primitives (1.2/1.3) ---


def test_reserve_convert_release_lifecycle():
    assert active_calls.try_acquire_slot(1)
    assert active_calls.reserved_slot_count() == 1
    active_calls.register_active_call(5, reserved=True)
    assert active_calls.reserved_slot_count() == 0
    assert active_calls.livekit_active_call_count() == 1
    assert active_calls.active_call_count() == 1
    active_calls.unregister_active_call(5)
    assert active_calls.livekit_active_call_count() == 0
    assert active_calls.try_acquire_slot(1)


def test_no_over_admission_at_limit():
    assert active_calls.try_acquire_slot(1)
    assert not active_calls.try_acquire_slot(1)


def test_reserved_and_active_both_count_against_limit():
    assert active_calls.try_acquire_slot(2)
    active_calls.register_active_call(5, reserved=True)
    assert active_calls.try_acquire_slot(2)
    assert not active_calls.try_acquire_slot(2)


def test_other_transports_do_not_consume_slots():
    # telephony/smallwebrtc register without reserved — never touch the gate
    active_calls.register_active_call(1)
    active_calls.register_active_call(2)
    assert active_calls.livekit_active_call_count() == 0
    assert active_calls.try_acquire_slot(1)


def test_no_reserved_underflow_from_unreserved_paths():
    active_calls.release_slot()  # nothing reserved — must stay at zero
    assert active_calls.reserved_slot_count() == 0
    active_calls.register_active_call(1)  # gate-disabled / other-transport shape
    active_calls.unregister_active_call(1)
    assert active_calls.reserved_slot_count() == 0
    # reserved=True without a prior acquire must not go negative either
    active_calls.register_active_call(2, reserved=True)
    assert active_calls.reserved_slot_count() == 0


# --- gate decision (2.3: 營運中 ∧ 隊列健康, shared functions) ---


def _patch_transfer_config(monkeypatch, config):
    from api.db import db_client
    from api.services.pipecat import transfer_call_config

    async def fake_get_workflow(workflow_id, user_id):
        return types.SimpleNamespace(organization_id=9, nodes={})

    async def fake_find(workflow, organization_id):
        return config

    monkeypatch.setattr(db_client, "get_workflow", fake_get_workflow)
    monkeypatch.setattr(transfer_call_config, "find_transfer_call_config", fake_find)


@pytest.mark.asyncio
async def test_gate_allows_when_unconfigured(monkeypatch):
    from api.db import db_client

    async def fake_get_workflow(workflow_id, user_id):
        return None

    monkeypatch.setattr(db_client, "get_workflow", fake_get_workflow)
    assert await capacity_gate._gate_allows(1, 2, datetime.now(timezone.utc))


@pytest.mark.asyncio
async def test_gate_blocks_outside_business_hours(monkeypatch):
    _patch_transfer_config(
        monkeypatch, {"schedule": {"tz": "UTC", "mon": [["09:00", "10:00"]]}}
    )
    tuesday = datetime(2026, 7, 21, 9, 30, tzinfo=timezone.utc)
    assert not await capacity_gate._gate_allows(1, 2, tuesday)


@pytest.mark.asyncio
async def test_gate_blocks_when_queue_unhealthy(monkeypatch):
    from api.services.pipecat import queue_health

    config = {"queueHealthUrl": "http://queue.internal/health"}
    _patch_transfer_config(monkeypatch, config)
    seen = {}

    async def fake_health(cfg):
        seen["config"] = cfg
        return False

    monkeypatch.setattr(queue_health, "queue_is_healthy", fake_health)
    assert not await capacity_gate._gate_allows(1, 2, datetime.now(timezone.utc))
    assert seen["config"] == config


# --- overflow action chain (2.3/2.4) ---


@pytest.mark.asyncio
async def test_overflow_transfers_caller_with_identity(monkeypatch, events, gate_open):
    monkeypatch.setenv("CAPACITY_OVERFLOW_TRANSFER_TO", "tel:+886900000000")
    lk, cap = _fake_lk([_sip_caller()])
    await _overflow("cs-+886912", lk)
    assert cap["transfers"] == ["tel:+886900000000"]
    assert cap["identities"] == ["sip_abc"]
    assert cap["deleted"] == []
    assert events == [
        {
            "event": "capacity.rejected",
            "room_name": "cs-+886912",
            "reason": "capacity",
            "active": 6,
            "limit": 6,
            "outcome": "transferred",
        }
    ]


@pytest.mark.asyncio
async def test_overflow_gate_closed_deletes_room(monkeypatch, events):
    monkeypatch.setenv("CAPACITY_OVERFLOW_TRANSFER_TO", "tel:+886900000000")

    async def deny(workflow_id, user_id, now):
        return False

    monkeypatch.setattr(capacity_gate, "_gate_allows", deny)
    lk, cap = _fake_lk([_sip_caller()])
    await _overflow("cs-+886912", lk)
    assert cap["transfers"] == []
    assert cap["deleted"] == ["cs-+886912"]
    assert events[-1]["outcome"] == "terminated"
    assert events[-1]["reason"] == "gate_closed"


@pytest.mark.asyncio
async def test_overflow_no_target_deletes_room(events, gate_open):
    lk, cap = _fake_lk([_sip_caller()])
    await _overflow("cs-+886912", lk)
    assert cap["transfers"] == []
    assert cap["deleted"] == ["cs-+886912"]
    assert events[-1]["outcome"] == "terminated"
    assert events[-1]["reason"] == "no_target"


@pytest.mark.asyncio
async def test_overflow_refer_failure_deletes_room(monkeypatch, events, gate_open):
    monkeypatch.setenv("CAPACITY_OVERFLOW_TRANSFER_TO", "tel:+886900000000")
    lk, cap = _fake_lk([_sip_caller()], raise_on_transfer=True)
    await _overflow("cs-+886912", lk)
    assert cap["deleted"] == ["cs-+886912"]
    assert events[-1]["outcome"] == "terminated"
    assert events[-1]["reason"] == "sip_refer_error"


@pytest.mark.asyncio
async def test_overflow_poll_exhausted_deletes_room(monkeypatch, events, gate_open):
    from api.services.pipecat import livekit_cold_transfer

    monkeypatch.setenv("CAPACITY_OVERFLOW_TRANSFER_TO", "tel:+886900000000")
    monkeypatch.setattr(livekit_cold_transfer, "WAIT_SIP_INTERVAL_SECONDS", 0.0)
    lk, cap = _fake_lk([])  # SIP caller never appears
    await _overflow("cs-+886912", lk)
    assert cap["transfers"] == []
    assert cap["deleted"] == ["cs-+886912"]
    assert events[-1]["outcome"] == "terminated"
    assert events[-1]["reason"] == "no_sip_caller"


@pytest.mark.asyncio
async def test_redelivered_trigger_blocked_by_guard(monkeypatch, events, gate_open):
    monkeypatch.setenv("CAPACITY_OVERFLOW_TRANSFER_TO", "tel:+886900000000")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocking_list(req):
        entered.set()
        await release.wait()
        return types.SimpleNamespace(participants=[_sip_caller()])

    lk, cap = _fake_lk(on_list=blocking_list)
    first = asyncio.create_task(_overflow("cs-+886912", lk))
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    async def must_not_touch(req):
        raise AssertionError("redelivered trigger must not act on the room")

    lk2, cap2 = _fake_lk(on_list=must_not_touch)
    await _overflow("cs-+886912", lk2)  # guard: no second REFER/delete/event
    assert events == []
    assert cap2["deleted"] == []

    release.set()
    await asyncio.wait_for(first, timeout=1.0)
    assert cap["transfers"] == ["tel:+886900000000"]
    assert len(events) == 1


@pytest.mark.asyncio
async def test_same_did_sequential_overflows_not_poisoned(
    monkeypatch, events, gate_open
):
    # Room names repeat per DID (cs-{call.to}) — the guard must release after
    # each completed action, or the first overflow poisons the number (F1).
    monkeypatch.setenv("CAPACITY_OVERFLOW_TRANSFER_TO", "tel:+886900000000")
    lk, cap = _fake_lk([_sip_caller()])
    await _overflow("cs-+886912", lk)
    await _overflow("cs-+886912", lk)
    assert cap["transfers"] == ["tel:+886900000000", "tel:+886900000000"]
    assert [e["outcome"] for e in events] == ["transferred", "transferred"]


@pytest.mark.asyncio
async def test_inflight_cap_exceeded_skips_to_delete(monkeypatch, events, gate_open):
    monkeypatch.setenv("CAPACITY_OVERFLOW_TRANSFER_TO", "tel:+886900000000")
    monkeypatch.setenv("CAPACITY_OVERFLOW_MAX_INFLIGHT", "1")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocking_list(req):
        entered.set()
        await release.wait()
        return types.SimpleNamespace(participants=[_sip_caller()])

    lk, cap = _fake_lk(on_list=blocking_list)
    first = asyncio.create_task(_overflow("cs-+886911", lk))
    await asyncio.wait_for(entered.wait(), timeout=1.0)

    async def must_not_poll(req):
        raise AssertionError("flood leg must skip polling")

    lk2, cap2 = _fake_lk(on_list=must_not_poll)
    await _overflow("cs-+886922", lk2)
    assert cap2["deleted"] == ["cs-+886922"]
    assert events[-1]["outcome"] == "terminated"
    assert events[-1]["reason"] == "overflow_flood"

    release.set()
    await asyncio.wait_for(first, timeout=1.0)
    assert cap["transfers"] == ["tel:+886900000000"]


# --- dispatch admission (2.1/2.4) ---


def _fill_slots(n):
    for i in range(n):
        assert active_calls.try_acquire_slot(n)
        active_calls.register_active_call(1000 + i, reserved=True)


def _wire_dispatch(monkeypatch, created, piped):
    from api.db import db_client
    from api.services.pipecat import livekit_dispatcher, run_pipeline

    async def fake_create(**kw):
        created.append(kw)
        return types.SimpleNamespace(id=77)

    async def fake_pipeline(**kw):
        piped.update(kw)

    monkeypatch.setattr(db_client, "create_workflow_run", fake_create)
    monkeypatch.setattr(run_pipeline, "run_pipeline_livekit", fake_pipeline)
    monkeypatch.setattr(livekit_dispatcher, "_sign_agent_token", lambda r, i: "jwt")
    monkeypatch.setenv("LIVEKIT_URL", "ws://test")


async def _resolver(did):
    return (1, 2)


async def _fallback_fails(room, reason, workflow_run_id=None):
    raise AssertionError(f"fallback must not fire: {reason}")


@pytest.mark.asyncio
async def test_full_capacity_skips_run_and_spawns_overflow(monkeypatch):
    from api.services.pipecat import livekit_dispatcher

    _fill_slots(6)
    called = {}

    async def fake_overflow(room_name, **kw):
        called.update(room=room_name, **kw)

    monkeypatch.setattr(capacity_gate, "capacity_overflow", fake_overflow)
    created, piped = [], {}
    _wire_dispatch(monkeypatch, created, piped)

    await livekit_dispatcher.dispatch_livekit_call(
        "cs-+886912345678", _resolver, _fallback_fails
    )
    await _drain()
    assert created == [] and piped == {}
    assert called["room"] == "cs-+886912345678"
    assert called["active"] == 6 and called["limit"] == 6
    assert called["workflow_id"] == 1 and called["user_id"] == 2


@pytest.mark.asyncio
async def test_dispatch_acks_before_overflow_completes(monkeypatch):
    from api.services.pipecat import livekit_dispatcher

    _fill_slots(6)
    entered = asyncio.Event()
    release = asyncio.Event()
    done = {}

    async def slow_overflow(room_name, **kw):
        entered.set()
        await release.wait()
        done["finished"] = True

    monkeypatch.setattr(capacity_gate, "capacity_overflow", slow_overflow)

    # dispatch must return while the overflow action chain is still running —
    # if it awaited the chain this would deadlock and time out
    await asyncio.wait_for(
        livekit_dispatcher.dispatch_livekit_call(
            "cs-+886912345678", _resolver, _fallback_fails
        ),
        timeout=1.0,
    )
    assert "finished" not in done
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    release.set()
    await _drain()
    assert done.get("finished") is True


@pytest.mark.asyncio
async def test_dispatch_resumes_after_slot_release(monkeypatch):
    from api.services.pipecat import livekit_dispatcher

    monkeypatch.setenv("LIVEKIT_MAX_CONCURRENT_CALLS", "1")
    _fill_slots(1)
    overflowed = []

    async def fake_overflow(room_name, **kw):
        overflowed.append(room_name)

    monkeypatch.setattr(capacity_gate, "capacity_overflow", fake_overflow)
    created, piped = [], {}
    _wire_dispatch(monkeypatch, created, piped)

    await livekit_dispatcher.dispatch_livekit_call(
        "cs-+886912345678", _resolver, _fallback_fails
    )
    await _drain()
    assert overflowed == ["cs-+886912345678"] and created == []

    active_calls.unregister_active_call(1000)  # the occupying call ends
    await livekit_dispatcher.dispatch_livekit_call(
        "cs-+886912345678", _resolver, _fallback_fails
    )
    await _drain()
    assert len(created) == 1
    assert piped.get("reserved") is True


@pytest.mark.asyncio
async def test_gate_disabled_dispatch_unchanged(monkeypatch):
    from api.services.pipecat import livekit_dispatcher

    monkeypatch.setenv("LIVEKIT_MAX_CONCURRENT_CALLS", "0")

    async def no_overflow(room_name, **kw):
        raise AssertionError("gate disabled — overflow must never trigger")

    monkeypatch.setattr(capacity_gate, "capacity_overflow", no_overflow)
    created, piped = [], {}
    _wire_dispatch(monkeypatch, created, piped)

    await livekit_dispatcher.dispatch_livekit_call(
        "cs-+886912345678", _resolver, _fallback_fails
    )
    await _drain()
    assert len(created) == 1
    assert piped.get("reserved") is False
    assert active_calls.reserved_slot_count() == 0


@pytest.mark.asyncio
async def test_midway_failure_releases_reservation(monkeypatch):
    from api.db import db_client
    from api.services.pipecat import livekit_dispatcher

    async def broken_create(**kw):
        raise RuntimeError("db blip")

    monkeypatch.setattr(db_client, "create_workflow_run", broken_create)
    fb = {}

    async def fallback(room, reason, workflow_run_id=None):
        fb["reason"] = reason

    await livekit_dispatcher.dispatch_livekit_call(
        "cs-+886912345678", _resolver, fallback
    )
    assert fb == {"reason": "launch_failed"}
    assert active_calls.reserved_slot_count() == 0  # no slot leak (D2 try/finally)
