"""The answered exit at both engine-free call sites (answer-before-refer §3).

``answer_then_exit`` (3.1), the dispatch-face safetynet (3.2/3.3), the
reconciler's skip of rooms being answered (3.4) and capacity overflow
(3.5/3.6). The answering participant is the shared fake; one ordered log
holds its plays and the fake LiveKit API's REFERs and deletes, so every
test can assert *order* — above all "room deleted before disconnect".
"""

import asyncio
import types

import pytest

from api.services.observability import call_events
from api.services.pipecat import capacity_gate
from api.services.pipecat import livekit_safetynet as sn
from api.services.pipecat.livekit_cold_transfer import SIP_KIND
from api.tests.support.answer_fakes import install

ROOM = "cs-_+886212345678_abc"
FALLBACK = "tel:+886287654321"


@pytest.fixture(autouse=True)
def _reset():
    sn._fired_runs.clear()
    sn._fired_order.clear()
    capacity_gate._overflow_in_progress.clear()
    yield


@pytest.fixture
def log():
    return []


@pytest.fixture
def answer(monkeypatch, log):
    return install(monkeypatch, log)


@pytest.fixture
def events(monkeypatch):
    captured = []

    def emit(event, **fields):
        captured.append({"event": event, **fields})

    monkeypatch.setattr(call_events, "emit", emit)
    monkeypatch.setattr(
        sn, "log_event", lambda event, **f: captured.append({"event": event, **f})
    )
    return captured


@pytest.fixture(autouse=True)
def no_outcome_db(monkeypatch):
    recorded = []

    async def record(engine, run_id, **kw):
        recorded.append((run_id, kw["outcome"]))

    monkeypatch.setattr(sn, "record_call_outcome", record)
    return recorded


def _lk(log, *, sip=True, refer_error=None, delete_error=None, refer_hang=False):
    async def list_participants(req):
        p = [types.SimpleNamespace(kind=SIP_KIND, identity="sip_abc")] if sip else []
        return types.SimpleNamespace(participants=p)

    async def transfer(req):
        log.append(("refer", req.transfer_to))
        if refer_hang:
            await asyncio.Event().wait()
        if refer_error is not None:
            raise refer_error

    async def delete_room(req):
        if delete_error is not None:
            log.append(("delete_failed", req.room))
            raise delete_error
        log.append(("delete", req.room))

    return types.SimpleNamespace(
        room=types.SimpleNamespace(
            list_participants=list_participants, delete_room=delete_room
        ),
        sip=types.SimpleNamespace(transfer_sip_participant=transfer),
    )


def _names(events):
    return [e["event"] for e in events]


# --- 3.1 answer_then_exit -----------------------------------------------------------


async def _exit(log, destination=FALLBACK, **lk_kw):
    return await sn.answer_then_exit(
        ROOM,
        client=_lk(log, **lk_kw),
        identity="sip_abc",
        destination=destination,
        reason="unmapped_did",
        workflow_run_id=None,
    )


async def test_exit_transfer_success_keeps_room(answer, log, events):
    ex = await _exit(log)
    assert ex.kind == "transferred"
    assert log == [
        ("answer", ROOM),
        ("play", "transfer"),
        ("refer", FALLBACK),
        ("disconnect", ROOM),
    ]


async def test_exit_refer_failure_plays_end_then_deletes_before_disconnect(
    answer, log, events
):
    ex = await _exit(log, refer_error=RuntimeError("403"))
    assert ex == sn.AnswerExit("refer_failed", refer_reason="sip_refer_error")
    assert log == [
        ("answer", ROOM),
        ("play", "transfer"),
        ("refer", FALLBACK),
        ("play", "end"),
        ("delete", ROOM),
        ("disconnect", ROOM),
    ]


async def test_exit_no_destination_plays_end_then_deletes(answer, log, events):
    ex = await _exit(log, destination=None)
    assert ex.kind == "ended"
    assert log == [
        ("answer", ROOM),
        ("play", "end"),
        ("delete", ROOM),
        ("disconnect", ROOM),
    ]


async def test_exit_unexpected_exception_still_deletes(answer, log, events):
    """AC11: a non-AnswerFailed exception after answering → room deleted."""
    answer.raise_at["transfer"] = KeyError("boom")
    ex = await _exit(log)
    assert ex.kind == "answer_failed" and ex.stage == "error"
    assert log == [("answer", ROOM), ("delete", ROOM), ("disconnect", ROOM)]


async def test_exit_cancelled_after_answer_finishes_the_delete(answer, log, events):
    """AC11: the task cancelled mid-prompt → the shielded delete completes."""
    answer.hang_at.add("transfer")
    task = asyncio.create_task(_exit(log))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert log == [("answer", ROOM), ("delete", ROOM), ("disconnect", ROOM)]


async def test_exit_delete_survives_a_second_cancel(answer, log, events, monkeypatch):
    """The shield: a cancel landing during the delete does not abort it."""
    answer.hang_at.add("transfer")
    gate = asyncio.Event()
    lk = _lk(log)
    real_delete = lk.room.delete_room

    async def slow_delete(req):
        await gate.wait()
        await real_delete(req)

    lk.room.delete_room = slow_delete
    task = asyncio.create_task(
        sn.answer_then_exit(
            ROOM, client=lk, identity="sip_abc", destination=FALLBACK, reason="x"
        )
    )
    await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()  # second cancel while the delete is in flight
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.01)
    assert ("delete", ROOM) in log


async def test_exit_delete_failure_pages(answer, log, events):
    """AC11: delete failure after answering → livekit.room_delete_failed."""
    ex = await _exit(log, destination=None, delete_error=RuntimeError("twirp 503"))
    assert ex.kind == "ended"
    failed = [e for e in events if e["event"] == "livekit.room_delete_failed"]
    assert failed and failed[0]["room_name"] == ROOM


async def test_exit_refused_before_join_still_deletes(answer, log, events):
    answer.fail_at["enter"] = "saturated"
    ex = await _exit(log)
    assert ex == sn.AnswerExit("answer_failed", stage="saturated")
    assert log == [("delete", ROOM)]  # never answered: this delete is the 486


@pytest.mark.parametrize("stage", ["connect", "publish"])
async def test_exit_failed_join_deletes_before_disconnect(answer, log, events, stage):
    """Review M1: livekit-sip may already have answered a join that then
    failed (a publish that timed out on our side but landed) — the room is
    deleted before the participant leaves, never after."""
    answer.fail_at["join"] = stage
    ex = await _exit(log)
    assert ex == sn.AnswerExit("answer_failed", stage=stage)
    assert log == [("answer", ROOM), ("delete", ROOM), ("disconnect", ROOM)]


async def test_exit_caller_left_never_refers(answer, log, events):
    answer.fail_at["transfer"] = "caller_left"
    ex = await _exit(log)
    assert ex.kind == "caller_left"
    assert ("refer", FALLBACK) not in log
    assert log[-2:] == [("delete", ROOM), ("disconnect", ROOM)]


async def test_exit_end_prompt_failure_after_refer_failure_keeps_refer_reason(
    answer, log, events
):
    answer.fail_at["end"] = "play"
    ex = await _exit(log, refer_error=RuntimeError("403"))
    assert ex == sn.AnswerExit(
        "answer_failed", stage="play", refer_reason="sip_refer_error"
    )
    assert ("delete", ROOM) in log


async def test_delete_not_found_is_not_a_failure(events):
    class NotFound(Exception):
        code = "not_found"

    lk = _lk([], delete_error=NotFound())
    await sn.delete_room(ROOM, lk)
    assert events == []


# --- 3.2 / 3.3 server_side_safetynet --------------------------------------------------


async def _safetynet(log, reason="unmapped_did", run_id=None, **lk_kw):
    await sn.server_side_safetynet(
        ROOM, reason, workflow_run_id=run_id, lk=_lk(log, **lk_kw)
    )


async def test_safetynet_answers_then_refers(
    answer, log, events, monkeypatch, no_outcome_db
):
    monkeypatch.setenv("SAFETYNET_FALLBACK_QUEUE", FALLBACK)
    await _safetynet(log, run_id=7)
    assert log == [
        ("answer", ROOM),
        ("play", "transfer"),
        ("refer", FALLBACK),
        ("disconnect", ROOM),
    ]
    assert _names(events) == ["safetynet.triggered", "safetynet.transfer_ok"]
    assert no_outcome_db == [(7, "transferred:safetynet")]


async def test_safetynet_refer_failure(answer, log, events, monkeypatch, no_outcome_db):
    monkeypatch.setenv("SAFETYNET_FALLBACK_QUEUE", FALLBACK)
    await _safetynet(log, refer_error=RuntimeError("403"))
    assert [x for x in log if x[0] != "answer"] == [
        ("play", "transfer"),
        ("refer", FALLBACK),
        ("play", "end"),
        ("delete", ROOM),
        ("disconnect", ROOM),
    ]
    assert _names(events) == [
        "safetynet.triggered",
        "safetynet.transfer_failed",
        "safetynet.terminated",
    ]
    assert events[1]["reason"] == "sip_refer_error"
    assert no_outcome_db == [(None, "safetynet_terminated")]


async def test_safetynet_no_destination_plays_end(answer, log, events, monkeypatch):
    monkeypatch.delenv("SAFETYNET_FALLBACK_QUEUE", raising=False)
    await _safetynet(log, reason="no_did")
    assert log == [
        ("answer", ROOM),
        ("play", "end"),
        ("delete", ROOM),
        ("disconnect", ROOM),
    ]
    assert _names(events) == ["safetynet.triggered", "safetynet.terminated"]


@pytest.mark.parametrize("stage", ["connect", "publish", "disabled", "saturated"])
async def test_safetynet_answer_unavailable_deletes(
    answer, log, events, monkeypatch, stage
):
    monkeypatch.setenv("SAFETYNET_FALLBACK_QUEUE", FALLBACK)
    step = "join" if stage in ("connect", "publish") else "enter"
    answer.fail_at[step] = stage
    await _safetynet(log)
    assert ("refer", FALLBACK) not in log  # no REFER without a played prompt
    assert ("delete", ROOM) in log
    assert _names(events) == [
        "safetynet.triggered",
        "safetynet.transfer_failed",
        "safetynet.terminated",
    ]
    assert events[1]["reason"] == f"answer_{stage}"


async def test_safetynet_no_sip_caller_never_answers(answer, log, events, monkeypatch):
    from api.services.pipecat import livekit_cold_transfer

    monkeypatch.setattr(livekit_cold_transfer, "WAIT_SIP_INTERVAL_SECONDS", 0.0)
    monkeypatch.setenv("SAFETYNET_FALLBACK_QUEUE", FALLBACK)
    await _safetynet(log, sip=False)
    assert answer.entered == []
    assert log == [("delete", ROOM)]
    assert events[1]["reason"] == "no_sip_caller"


async def test_safetynet_caller_left_is_not_a_failure(
    answer, log, events, monkeypatch, no_outcome_db
):
    """AC13: no REFER, room deleted, no transfer_failed / terminated alerts."""
    monkeypatch.setenv("SAFETYNET_FALLBACK_QUEUE", FALLBACK)
    answer.fail_at["transfer"] = "caller_left"
    await _safetynet(log)
    assert ("refer", FALLBACK) not in log and ("delete", ROOM) in log
    assert _names(events) == ["safetynet.triggered"]
    assert no_outcome_db == []


async def test_safetynet_catch_all_now_deletes(answer, log, events, monkeypatch):
    """The outer catch-all used to only log, leaving the room (design D6)."""
    monkeypatch.setenv("SAFETYNET_FALLBACK_QUEUE", FALLBACK)

    async def broken_wait(*a, **kw):
        raise RuntimeError("list broke")

    from api.services.pipecat import livekit_cold_transfer

    monkeypatch.setattr(livekit_cold_transfer, "wait_for_sip_participant", broken_wait)
    await _safetynet(log)
    assert log == [("delete", ROOM)]
    assert events[-1]["reason"] == "safetynet_error"


async def test_safetynet_reentry_same_run_answers_once(
    answer, log, events, monkeypatch
):
    """AC8 with a run: the latch stops a second answering participant."""
    monkeypatch.setenv("SAFETYNET_FALLBACK_QUEUE", FALLBACK)
    answer.hang_at.add("transfer")
    first = asyncio.create_task(_safetynet(log, reason="launch_failed", run_id=9))
    await asyncio.sleep(0.01)
    await _safetynet(log, reason="pipeline_exception", run_id=9)
    assert answer.entered == [ROOM]
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert log.count(("refer", FALLBACK)) == 0


async def test_no_did_redelivery_blocked_by_dedup():
    """AC8 without a run: the dispatcher's per-call dedup is what stops a
    redelivered no_did trigger — the safetynet latch does not apply."""
    from api.services.pipecat.livekit_dispatcher import DispatchDedup

    dedup = DispatchDedup()
    assert dedup.claim("SCL_1")
    dedup.commit("SCL_1")
    assert not dedup.claim("SCL_1")
    assert sn.claim(None) and sn.claim(None)  # pre-run: never latched


async def test_midcall_safetynet_error_takes_the_same_exit(
    answer, log, events, monkeypatch
):
    monkeypatch.setenv("SAFETYNET_FALLBACK_QUEUE", FALLBACK)
    lk = _lk(log)
    from api.services.pipecat import livekit_cold_transfer

    real_api = livekit_cold_transfer.livekit_api

    def fixed_api(client=None):
        return real_api(lk)

    monkeypatch.setattr(livekit_cold_transfer, "livekit_api", fixed_api)

    async def broken(*a, **kw):
        raise RuntimeError("engine half dead")

    from api.services.pipecat import livekit_transfer_flow

    monkeypatch.setattr(livekit_transfer_flow, "execute_cold_transfer", broken)
    engine = types.SimpleNamespace(task=types.SimpleNamespace(queue_frame=broken))
    await sn.midcall_safetynet(
        engine, room_name=ROOM, reason="bot_silence", workflow_run_id=11
    )
    assert log == [
        ("answer", ROOM),
        ("play", "transfer"),
        ("refer", FALLBACK),
        ("disconnect", ROOM),
    ]
    triggered = [e for e in events if e["event"] == "safetynet.triggered"]
    assert [e["reason"] for e in triggered] == [
        "bot_silence",
        "midcall_safetynet_error",
    ]


# --- 3.4 reconciler -------------------------------------------------------------------


NOW = 1_791_250_000.0


def _recon_lk(rooms):
    async def list_rooms(req):
        return types.SimpleNamespace(
            rooms=[types.SimpleNamespace(name=n) for n in rooms]
        )

    async def list_participants(req):
        return types.SimpleNamespace(
            participants=[
                types.SimpleNamespace(identity=i, kind=k, joined_at=j, attributes=a)
                for i, k, j, a in rooms[req.room]
            ]
        )

    return types.SimpleNamespace(
        room=types.SimpleNamespace(
            list_rooms=list_rooms, list_participants=list_participants
        )
    )


async def test_reconcile_skips_room_being_answered(monkeypatch):
    from api.services.pipecat.livekit_dispatcher import DispatchDedup

    handed = []

    async def fake_safetynet(room, reason, run_id=None, lk=None):
        handed.append(room)

    monkeypatch.setattr(sn, "server_side_safetynet", fake_safetynet)
    lk = _recon_lk(
        {
            ROOM: [
                ("sip_x", SIP_KIND, NOW - 60, {"sip.callID": "SCL_1"}),
                ("agent-answer-ab12cd34", 0, NOW - 50, {}),
            ]
        }
    )
    assert (
        await sn.reconcile_undispatched_rooms(lk, now=NOW, dedup=DispatchDedup()) == 0
    )
    assert handed == []


async def test_reconcile_hand_off_answers(answer, log, events, monkeypatch):
    """The reconciler's hand-off is the safetynet, so a stranded caller is
    answered and hears the transfer prompt before the REFER (AC3 unit)."""
    from api.services.pipecat import livekit_cold_transfer
    from api.services.pipecat.livekit_dispatcher import DispatchDedup

    monkeypatch.setenv("SAFETYNET_FALLBACK_QUEUE", FALLBACK)
    act = _lk(log)
    recon = _recon_lk(
        {ROOM: [("sip_abc", SIP_KIND, NOW - 20, {"sip.callID": "SCL_1"})]}
    )
    recon.room.delete_room = act.room.delete_room
    recon.sip = act.sip
    monkeypatch.setattr(livekit_cold_transfer, "WAIT_SIP_INTERVAL_SECONDS", 0.0)

    assert (
        await sn.reconcile_undispatched_rooms(recon, now=NOW, dedup=DispatchDedup())
        == 1
    )
    assert log == [
        ("answer", ROOM),
        ("play", "transfer"),
        ("refer", FALLBACK),
        ("disconnect", ROOM),
    ]
    assert events[0]["reason"] == "undispatched"


# --- 3.5 / 3.6 capacity overflow ------------------------------------------------------


@pytest.fixture
def gate(monkeypatch):
    verdict = {"open": True}

    async def allows(workflow_id, user_id, now):
        return verdict["open"]

    monkeypatch.setattr(capacity_gate, "_gate_allows", allows)
    return verdict


async def _overflow(lk, room=ROOM):
    await capacity_gate.capacity_overflow(
        room, active=6, limit=6, workflow_id=1, user_id=1, lk=lk
    )


def _rejected(events):
    (ev,) = [e for e in events if e["event"] == "capacity.rejected"]
    return ev["outcome"], ev["reason"]


async def test_overflow_transfers_after_prompt(answer, log, events, gate, monkeypatch):
    monkeypatch.setenv("CAPACITY_OVERFLOW_TRANSFER_TO", FALLBACK)
    await _overflow(_lk(log))
    assert log == [
        ("answer", ROOM),
        ("play", "transfer"),
        ("refer", FALLBACK),
        ("disconnect", ROOM),
    ]
    assert _rejected(events) == ("transferred", "capacity")


@pytest.mark.parametrize(
    "setup, expected_log, reason",
    [
        ("no_target", [("play", "end"), ("delete", ROOM)], "no_target"),
        ("gate_closed", [("play", "end"), ("delete", ROOM)], "gate_closed"),
        (
            "refer_failed",
            [
                ("play", "transfer"),
                ("refer", FALLBACK),
                ("play", "end"),
                ("delete", ROOM),
            ],
            "sip_refer_error",
        ),
    ],
)
async def test_overflow_end_branches(
    answer, log, events, gate, monkeypatch, setup, expected_log, reason
):
    monkeypatch.delenv("SAFETYNET_FALLBACK_QUEUE", raising=False)
    if setup == "no_target":
        monkeypatch.delenv("CAPACITY_OVERFLOW_TRANSFER_TO", raising=False)
    else:
        monkeypatch.setenv("CAPACITY_OVERFLOW_TRANSFER_TO", FALLBACK)
    gate["open"] = setup != "gate_closed"
    lk = _lk(log, refer_error=RuntimeError("403") if setup == "refer_failed" else None)
    await _overflow(lk)
    assert log == [("answer", ROOM), *expected_log, ("disconnect", ROOM)]
    assert _rejected(events) == ("terminated", reason)


async def test_overflow_flood_never_answers(answer, log, events, gate, monkeypatch):
    monkeypatch.setenv("CAPACITY_OVERFLOW_TRANSFER_TO", FALLBACK)
    monkeypatch.setenv("CAPACITY_OVERFLOW_MAX_INFLIGHT", "1")
    capacity_gate._overflow_in_progress.add("cs-other")
    await _overflow(_lk(log))
    assert answer.entered == []
    assert log == [("delete", ROOM)]
    assert _rejected(events) == ("terminated", "overflow_flood")


async def test_overflow_no_sip_caller_never_answers(
    answer, log, events, gate, monkeypatch
):
    from api.services.pipecat import livekit_cold_transfer

    monkeypatch.setattr(livekit_cold_transfer, "WAIT_SIP_INTERVAL_SECONDS", 0.0)
    monkeypatch.setenv("CAPACITY_OVERFLOW_TRANSFER_TO", FALLBACK)
    await _overflow(_lk(log, sip=False))
    assert answer.entered == [] and log == [("delete", ROOM)]
    assert _rejected(events) == ("terminated", "no_sip_caller")


@pytest.mark.parametrize("stage", ["connect", "saturated", "disabled"])
async def test_overflow_answer_unavailable(
    answer, log, events, gate, monkeypatch, stage
):
    monkeypatch.setenv("CAPACITY_OVERFLOW_TRANSFER_TO", FALLBACK)
    answer.fail_at["join" if stage == "connect" else "enter"] = stage
    await _overflow(_lk(log))
    assert ("refer", FALLBACK) not in log and ("delete", ROOM) in log
    assert _rejected(events) == ("terminated", f"answer_{stage}")


async def test_overflow_caller_left(answer, log, events, gate, monkeypatch):
    monkeypatch.setenv("CAPACITY_OVERFLOW_TRANSFER_TO", FALLBACK)
    answer.fail_at["transfer"] = "caller_left"
    await _overflow(_lk(log))
    assert ("refer", FALLBACK) not in log
    assert _rejected(events) == ("terminated", "caller_left")


async def test_overflow_timeout_after_answer_still_deletes(
    answer, log, events, gate, monkeypatch
):
    """overflow_timeout once answered: the room is deleted (BYE), guard freed."""
    monkeypatch.setenv("CAPACITY_OVERFLOW_TRANSFER_TO", FALLBACK)
    monkeypatch.setattr(capacity_gate, "overflow_action_timeout", lambda: 0.05)
    await asyncio.wait_for(_overflow(_lk(log, refer_hang=True)), 2.0)
    assert log[:3] == [("answer", ROOM), ("play", "transfer"), ("refer", FALLBACK)]
    assert ("delete", ROOM) in log
    assert log.index(("delete", ROOM)) < log.index(("disconnect", ROOM))
    assert _rejected(events) == ("terminated", "overflow_timeout")
    assert capacity_gate._overflow_in_progress == set()


async def test_overflow_guard_blocks_reentry(answer, log, events, gate, monkeypatch):
    monkeypatch.setenv("CAPACITY_OVERFLOW_TRANSFER_TO", FALLBACK)
    answer.hang_at.add("transfer")
    first = asyncio.create_task(_overflow(_lk(log)))
    await asyncio.sleep(0.01)
    await _overflow(_lk(log))  # redelivered trigger
    assert answer.entered == [ROOM]
    assert [e for e in events if e["event"] == "capacity.rejected"] == []
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first


async def test_overflow_same_did_sequential_not_poisoned(
    answer, log, events, gate, monkeypatch
):
    monkeypatch.setenv("CAPACITY_OVERFLOW_TRANSFER_TO", FALLBACK)
    await _overflow(_lk(log))
    await _overflow(_lk(log))
    assert answer.entered == [ROOM, ROOM]
    assert log.count(("refer", FALLBACK)) == 2


def test_overflow_timeout_is_derived_not_fixed(monkeypatch):
    """D5: gate + poll + answering hard cap + REFER (32 s) + delete — never
    below the stages it bounds, and it grows with the prompts."""
    from api.services.pipecat import livekit_answer as la

    monkeypatch.setattr(la, "_prompts", {})
    base = capacity_gate.overflow_action_timeout()
    assert base == pytest.approx(2 + 3 + la.hard_cap_seconds() + 32 + 10)
    pcm = b"\x00" * la.FRAME_BYTES * 50 * 3  # 3 s
    monkeypatch.setattr(la, "_prompts", {"transfer": pcm, "end": pcm})
    assert capacity_gate.overflow_action_timeout() == pytest.approx(base + 2 * (3 + 1))


# --- review H1: the overflow gate reads the workflow's node graph ---------------------


async def test_gate_lookup_gets_a_node_graph_not_the_db_row(monkeypatch):
    """Review H1: ``_gate_allows`` used to hand the DB row to the tool lookup,
    which walks ``.nodes`` → AttributeError on every call → the gate degraded
    to "open, health unchecked" and overflow REFERed into closed or dead
    queues. Not mocked here: the real ``_gate_allows`` with a real-shaped row."""
    from api.db import db_client
    from api.services.pipecat import livekit_transfer_flow, transfer_call_config
    from api.tests.support.workflow_rows import workflow_row

    async def get_workflow(workflow_id, user_id):
        return workflow_row(tool_uuid="xfer-1")

    seen = {}

    async def lookup(graph, organization_id):
        seen["tool_uuids"] = [
            tu
            for n in graph.nodes.values()
            for tu in (getattr(n, "tool_uuids", None) or [])
        ]
        return {"queueHealthUrl": "http://queue:8080/health"}

    async def decide(schedule, alt, now, config):
        seen["config"] = config
        return livekit_transfer_flow.TransferDecision.UNAVAILABLE

    unvalidatable = []
    monkeypatch.setattr(db_client, "get_workflow", get_workflow)
    monkeypatch.setattr(transfer_call_config, "find_transfer_call_config", lookup)
    monkeypatch.setattr(capacity_gate, "resolve_transfer_decision", decide)
    monkeypatch.setattr(
        transfer_call_config, "_config_event", lambda *a, **k: unvalidatable.append(a)
    )
    from datetime import datetime, timezone

    assert await capacity_gate._gate_allows(1, 1, datetime.now(timezone.utc)) is False
    assert seen["tool_uuids"] == ["xfer-1"]
    assert seen["config"] == {"queueHealthUrl": "http://queue:8080/health"}
    assert unvalidatable == []
