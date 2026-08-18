"""Shared cold-transfer preamble tests (S-L3-PRESS0 §3.6).

The planner suite is pure and runs anywhere. The executor suite exercises the
idempotency guard, REFER paths, and after-hours dispatch against a fake engine
and a fake LiveKitAPI; it needs the pipecat runtime (TTSSpeakFrame) so it is
skipped where pipecat is not installed (it runs in CI/Docker).
"""

import types
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from api.services.pipecat.livekit_transfer_flow import (
    TransferDecision,
    plan_transfer,
    valid_destination,
)

TPE = ZoneInfo("Asia/Taipei")
SCHED = {"tz": "Asia/Taipei", "mon": [["09:00", "18:00"]]}
OPEN = datetime(2026, 6, 29, 10, 0, tzinfo=TPE)  # Monday in hours
CLOSED = datetime(2026, 6, 29, 20, 0, tzinfo=TPE)  # Monday after hours

try:
    import pipecat.frames.frames  # noqa: F401

    PIPECAT = True
except ImportError:
    PIPECAT = False


# --- planner (pure) -------------------------------------------------------


def test_open_hours_always_refer():
    assert plan_transfer(SCHED, "announce_and_hangup", OPEN) is TransferDecision.REFER


def test_no_schedule_refers_failopen():
    assert plan_transfer(None, "announce_and_hangup", CLOSED) is TransferDecision.REFER


def test_after_hours_back_to_ai():
    assert (
        plan_transfer(SCHED, "back_to_ai", CLOSED)
        is TransferDecision.AFTER_HOURS_BACK_TO_AI
    )


def test_after_hours_hangup():
    assert (
        plan_transfer(SCHED, "announce_and_hangup", CLOSED)
        is TransferDecision.AFTER_HOURS_HANGUP
    )


def test_after_hours_alternate():
    assert (
        plan_transfer(SCHED, "alternate_queue", CLOSED)
        is TransferDecision.AFTER_HOURS_ALTERNATE
    )


def test_after_hours_unknown_falls_back_to_default():
    assert (
        plan_transfer(SCHED, "carrier_pigeon", CLOSED)
        is TransferDecision.AFTER_HOURS_BACK_TO_AI
    )


def test_after_hours_unset_falls_back_to_default():
    assert plan_transfer(SCHED, None, CLOSED) is TransferDecision.AFTER_HOURS_BACK_TO_AI


def test_valid_destination():
    assert valid_destination("tel:+886912345678")
    assert valid_destination("sip:queue@pbx.example")
    assert not valid_destination("0912345678")  # caller-shaped, not config-shaped
    assert not valid_destination("")
    assert not valid_destination(None)


# The two layers that must never drift apart: whatever the executor will dial,
# the API write path has to accept. They diverged once into an empty
# intersection — every shape the schema took, the executor refused — and the
# only symptom was press-0 quietly not installing.
LIVEKIT_DESTINATIONS = [
    "tel:+886912345678",
    "tel:+12025550123",
    "sip:queue@pbx.example",
    "sip:human-queue@10.0.0.1",
    "sip:queue@pbx.example:5060",
]


@pytest.mark.parametrize("destination", LIVEKIT_DESTINATIONS)
def test_schema_accepts_livekit_destinations(destination):
    """Every canonically-shaped executor target is writable, for both fields.

    Not the full ``executor-valid ⊆ schema-valid`` inclusion: the executor
    ``.strip()``s before matching, so `` tel:+886912345678`` is executor-valid
    and schema-invalid. That direction fails safe (unwritable, not undialable)
    and is left alone.
    """
    from api.schemas.tool import TransferCallConfig

    assert valid_destination(destination)
    cfg = TransferCallConfig(destination=destination, alternateDestination=destination)
    assert cfg.destination == destination
    assert cfg.alternateDestination == destination


def test_schema_still_accepts_ari_dialstrings():
    """The ARI dialect stays writable — the fix widened the union, not swapped it."""
    from api.schemas.tool import TransferCallConfig

    for destination in ("+886223456789", "PJSIP/1234", "SIP/human-queue@10.0.0.1"):
        assert TransferCallConfig(destination=destination).destination == destination


def test_schema_still_rejects_caller_shaped_destinations():
    from pydantic import ValidationError

    from api.schemas.tool import TransferCallConfig

    for destination in ("0912345678", "queue@pbx.example", "sip:", "tel:0912345678"):
        with pytest.raises(ValidationError):
            TransferCallConfig(destination=destination)


# --- guards that travel with the widened dialect --------------------------


@pytest.mark.parametrize(
    "destination",
    [
        "sip:queue@pbx.exampl\u0435",  # Cyrillic е — reads as pbx.example
        "sip:\uff51ueue@pbx.example",  # full-width q
        "tel:+886\u0662\u0663\u0664\u0665\u0666",  # Arabic-Indic digits
    ],
)
def test_both_layers_reject_homoglyph_hosts(destination):
    """``\\w`` was Unicode-aware, so these read as the real host and resolved elsewhere.

    Pinned on both layers because the schema copies the executor's shape, and
    both now track deploy/bin/feature_scope_check.py's canonical DESTINATION_RE.
    """
    from pydantic import ValidationError

    from api.schemas.tool import TransferCallConfig

    assert not valid_destination(destination)
    with pytest.raises(ValidationError):
        TransferCallConfig(destination=destination)


@pytest.mark.parametrize(
    "destination", ["tel:+886912345678\n", "sip:queue@pbx.example\n"]
)
def test_schema_rejects_trailing_newline(destination):
    """``$`` also matches before a trailing newline, so these used to be writable.

    The executor is deliberately *not* pinned to reject them: valid_destination
    normalises (``.strip()``) before matching, which is the right behaviour for
    a dial path. The write layer is where the value gets refused; the dial paths
    strip so nothing un-normalised reaches ``transfer_to``
    (test_alternate_destination_is_stripped_before_refer).
    """
    from pydantic import ValidationError

    from api.schemas.tool import TransferCallConfig

    with pytest.raises(ValidationError):
        TransferCallConfig(destination=destination)


@pytest.mark.parametrize(
    "destination",
    [
        "tel:+19005551212",
        "tel:+19765551212",
        "tel:+886204123456",
        "sip:1900555@pbx.example",
    ],
)
def test_schema_refuses_premium_rate_targets(destination):
    """The field every press-0 and every AI transfer dials gets capacity_gate's guard.

    capacity_gate refuses to boot on a premium-rate overflow target; this one is
    dialled more often, not less.
    """
    from pydantic import ValidationError

    from api.schemas.tool import TransferCallConfig

    with pytest.raises(ValidationError, match="premium-rate"):
        TransferCallConfig(destination=destination)


def test_premium_guard_uses_capacity_gate_as_the_single_source():
    """Same prefix list, not a second copy that can drift."""
    from pydantic import ValidationError

    from api.schemas.tool import TransferCallConfig
    from api.services.pipecat.capacity_gate import PREMIUM_RATE_PREFIXES

    for prefix in PREMIUM_RATE_PREFIXES:
        with pytest.raises(ValidationError, match="premium-rate"):
            TransferCallConfig(destination=f"tel:+{prefix}5551212")


# --- executor (needs pipecat) ---------------------------------------------


def _fake_engine():
    frames = []
    ended = []

    async def queue_frame(frame):
        frames.append(frame)

    async def end_call_with_reason(reason, abort_immediately=False):
        ended.append(reason)

    eng = types.SimpleNamespace(
        task=types.SimpleNamespace(queue_frame=queue_frame),
        end_call_with_reason=end_call_with_reason,
        _frames=frames,
        _ended=ended,
    )
    return eng


def _fake_lk(*, sip_caller=True, raise_on_transfer=False):
    from api.services.pipecat.livekit_cold_transfer import SIP_KIND

    captured = {}
    participants = (
        [types.SimpleNamespace(kind=SIP_KIND, identity="sip_caller")]
        if sip_caller
        else []
    )

    async def list_participants(req):
        return types.SimpleNamespace(participants=participants)

    async def transfer(req):
        if raise_on_transfer:
            raise RuntimeError("provider rejected")
        captured["transfer_to"] = req.transfer_to

    async def aclose():
        pass

    lk = types.SimpleNamespace(
        room=types.SimpleNamespace(list_participants=list_participants),
        sip=types.SimpleNamespace(transfer_sip_participant=transfer),
        aclose=aclose,
    )
    return lk, captured


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_open_refer_success_ends_call_and_runs_before_refer():
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    eng = _fake_engine()
    lk, cap = _fake_lk()
    announced = []

    async def before_refer():
        announced.append(True)

    res = await execute_cold_transfer(
        eng,
        room_name="room1",
        destination="tel:+886912345678",
        schedule=SCHED,
        before_refer=before_refer,
        now=OPEN,
        lk=lk,
    )
    assert res["status"] == "success"
    assert announced == [True]
    assert cap["transfer_to"] == "tel:+886912345678"
    assert eng._ended  # call ended on success
    assert eng._livekit_transfer_in_progress is False  # flag cleared


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_refer_failure_no_retry_no_end_call():
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    eng = _fake_engine()
    lk, _ = _fake_lk(raise_on_transfer=True)
    res = await execute_cold_transfer(
        eng,
        room_name="room1",
        destination="tel:+886912345678",
        schedule=SCHED,
        now=OPEN,
        lk=lk,
    )
    assert res["status"] == "failed"
    assert res["reason"] == "sip_refer_error"
    assert not eng._ended  # not ended; LLM informs and offers fallback
    assert eng._livekit_transfer_in_progress is False


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_invalid_destination_rejected():
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    eng = _fake_engine()
    res = await execute_cold_transfer(
        eng, room_name="room1", destination="0912", now=OPEN
    )
    assert res["reason"] == "invalid_destination"


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_idempotency_rejects_second_trigger():
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    eng = _fake_engine()
    eng._livekit_transfer_in_progress = True  # already transferring
    lk, _ = _fake_lk()
    res = await execute_cold_transfer(
        eng,
        room_name="room1",
        destination="tel:+886912345678",
        schedule=SCHED,
        now=OPEN,
        lk=lk,
    )
    assert res["reason"] == "already_transferring"


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_after_hours_back_to_ai_announces_no_transfer():
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    eng = _fake_engine()
    res = await execute_cold_transfer(
        eng,
        room_name="room1",
        destination="tel:+886912345678",
        schedule=SCHED,
        after_hours_action="back_to_ai",
        now=CLOSED,
    )
    assert res == {"status": "after_hours", "action": "back_to_ai"}
    assert eng._frames  # announcement queued
    assert not eng._ended


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_after_hours_hangup_announces_then_ends():
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    eng = _fake_engine()
    res = await execute_cold_transfer(
        eng,
        room_name="room1",
        destination="tel:+886912345678",
        schedule=SCHED,
        after_hours_action="announce_and_hangup",
        now=CLOSED,
    )
    assert res["action"] == "announced_hangup"
    assert eng._frames and eng._ended


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_after_hours_alternate_queue_refers_to_alternate():
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    eng = _fake_engine()
    lk, cap = _fake_lk()
    res = await execute_cold_transfer(
        eng,
        room_name="room1",
        destination="tel:+886900000000",
        schedule=SCHED,
        after_hours_action="alternate_queue",
        alternate_destination="sip:night@pbx.example",
        now=CLOSED,
        lk=lk,
    )
    assert res["status"] == "success"
    assert cap["transfer_to"] == "sip:night@pbx.example"  # alternate, not primary


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_alternate_destination_is_stripped_before_refer():
    """The alternate branch used to hand _do_refer the raw, unstripped value.

    valid_destination() strips only to judge, so a stored value with surrounding
    whitespace passed the check and then went to transfer_to verbatim. Every
    other dial path strips at the call site; this one did not.
    """
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    eng = _fake_engine()
    lk, cap = _fake_lk()
    res = await execute_cold_transfer(
        eng,
        room_name="room1",
        destination="tel:+886900000000",
        schedule=SCHED,
        after_hours_action="alternate_queue",
        alternate_destination="  sip:night@pbx.example\n",
        now=CLOSED,
        lk=lk,
    )
    assert res["status"] == "success"
    assert cap["transfer_to"] == "sip:night@pbx.example"


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_after_hours_alternate_without_target_falls_back_to_ai():
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    eng = _fake_engine()
    res = await execute_cold_transfer(
        eng,
        room_name="room1",
        destination="tel:+886900000000",
        schedule=SCHED,
        after_hours_action="alternate_queue",
        alternate_destination=None,
        now=CLOSED,
    )
    assert res == {"status": "after_hours", "action": "back_to_ai"}
    assert eng._frames and not eng._ended


# --- queue-health dimension (S-L5-QUEUE) -----------------------------------


def test_open_but_unhealthy_is_unavailable():
    assert (
        plan_transfer(SCHED, "back_to_ai", OPEN, queue_healthy=False)
        is TransferDecision.UNAVAILABLE
    )


def test_closed_and_unhealthy_stays_after_hours():
    # schedule wins first: out-of-hours handling is unchanged by health
    assert (
        plan_transfer(SCHED, "announce_and_hangup", CLOSED, queue_healthy=False)
        is TransferDecision.AFTER_HOURS_HANGUP
    )


def test_healthy_default_keeps_prior_behavior():
    assert plan_transfer(SCHED, None, OPEN) is TransferDecision.REFER


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_unavailable_announces_and_returns_to_ai(monkeypatch):
    from api.services.pipecat import queue_health as qh
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    async def always_unhealthy(config, **kw):
        return False

    # execute_cold_transfer imports the symbol lazily from the module, so
    # patching the module attribute is effective.
    monkeypatch.setattr(qh, "queue_is_healthy", always_unhealthy)
    eng = _fake_engine()
    result = await execute_cold_transfer(
        eng,
        room_name="cs-room",
        destination="tel:+886277001234",
        schedule=SCHED,
        now=OPEN,
        queue_health_config={"queueHealthUrl": "http://queue.internal/internal/health"},
    )

    assert result == {"status": "unavailable", "action": "back_to_ai"}
    assert len(eng._frames) == 1  # explicit spoken message, no REFER attempt
    assert eng._ended == []
    assert eng._transfer_unavailable_announcements == 1


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_unavailable_announce_limit_ends_call(monkeypatch):
    from api.services.pipecat import queue_health as qh
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    async def always_unhealthy(config, **kw):
        return False

    monkeypatch.setattr(qh, "queue_is_healthy", always_unhealthy)
    eng = _fake_engine()
    results = []
    for _ in range(3):
        results.append(
            await execute_cold_transfer(
                eng,
                room_name="cs-room",
                destination="tel:+886277001234",
                schedule=SCHED,
                now=OPEN,
                queue_health_config={"queueHealthUrl": "http://q/internal/health"},
                unavailable_announce_limit=2,
            )
        )

    assert [r["action"] for r in results] == [
        "back_to_ai",
        "back_to_ai",
        "announced_hangup",
    ]
    assert len(eng._ended) == 1  # third trigger ends the call explicitly (C4)


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_no_health_source_behavior_unchanged():
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    eng = _fake_engine()
    lk, captured = _fake_lk()
    result = await execute_cold_transfer(
        eng,
        room_name="cs-room",
        destination="tel:+886277001234",
        schedule=SCHED,
        now=OPEN,
        lk=lk,
        # queue_health_config deliberately omitted: unchecked -> straight REFER
    )
    assert result.get("status") == "success"
    assert captured["transfer_to"] == "tel:+886277001234"


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_malformed_announce_limit_degrades_to_default(monkeypatch):
    from api.services.pipecat import queue_health as qh
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    async def always_unhealthy(config, **kw):
        return False

    monkeypatch.setattr(qh, "queue_is_healthy", always_unhealthy)
    eng = _fake_engine()
    results = [
        await execute_cold_transfer(
            eng,
            room_name="cs-room",
            destination="tel:+886277001234",
            schedule=SCHED,
            now=OPEN,
            queue_health_config={"queueHealthUrl": "http://q/h"},
            unavailable_announce_limit="two",  # config typo: degrade, never raise (H1)
        )
        for _ in range(3)
    ]
    assert [r["action"] for r in results] == [
        "back_to_ai",
        "back_to_ai",
        "announced_hangup",  # default cap (2) still enforced
    ]


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_closed_hours_skips_health_probe(monkeypatch):
    from api.services.pipecat import queue_health as qh
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    probed = []

    async def recording_health(config, **kw):
        probed.append(config)
        return True

    monkeypatch.setattr(qh, "queue_is_healthy", recording_health)
    eng = _fake_engine()
    await execute_cold_transfer(
        eng,
        room_name="cs-room",
        destination="tel:+886277001234",
        schedule=SCHED,
        now=CLOSED,
        queue_health_config={"queueHealthUrl": "http://q/h"},
    )
    assert probed == []  # M1: closed hours never pay the probe latency


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_unavailable_emits_observability_event(monkeypatch):
    from api.services.observability import call_events
    from api.services.pipecat import queue_health as qh
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    async def always_unhealthy(config, **kw):
        return False

    events = []
    monkeypatch.setattr(qh, "queue_is_healthy", always_unhealthy)
    monkeypatch.setattr(
        call_events, "emit", lambda event, **fields: events.append((event, fields))
    )
    eng = _fake_engine()
    await execute_cold_transfer(
        eng,
        room_name="cs-room",
        destination="tel:+886277001234",
        schedule=SCHED,
        now=OPEN,
        queue_health_config={"queueHealthUrl": "http://q/h"},
        transfer_reason="voice_tool",
    )
    assert [e for e, _ in events] == ["transfer.unavailable"]  # M3: visible to ops
    assert events[0][1]["room_name"] == "cs-room"
    assert events[0][1]["transfer_reason"] == "voice_tool"
