"""Call observability tests (S-L7-OBS): unified events, alert routing,
window aggregation, call-outcome tagging, transfer-event emission."""

import asyncio
import types

import pytest

from api.services.observability import alerts, call_events
from api.services.observability.call_outcome import record_call_outcome


@pytest.fixture(autouse=True)
def _clean_alert_env(monkeypatch):
    monkeypatch.delenv("OBS_ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("OBS_ERROR_WINDOW_SECONDS", raising=False)
    monkeypatch.delenv("OBS_ERROR_THRESHOLD", raising=False)
    alerts._redis = None
    yield
    alerts._redis = None


@pytest.fixture
def sent(monkeypatch):
    captured = []

    async def fake_send(url, text):
        captured.append({"url": url, "text": text})

    monkeypatch.setattr(alerts, "_send", fake_send)
    return captured


async def _drain():
    for _ in range(5):
        await asyncio.sleep(0)


class _FakeRedis:
    def __init__(self):
        self.counts = {}
        self.expires = []
        self.deletes = []

    async def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key, seconds):
        self.expires.append((key, seconds))

    async def delete(self, key):
        self.deletes.append(key)
        self.counts.pop(key, None)


# --- emit / alert routing (1.1–1.3) ---


@pytest.mark.asyncio
async def test_emit_routes_to_alerts(monkeypatch):
    routed = []
    monkeypatch.setattr(
        alerts, "notify", lambda event, fields: routed.append((event, fields))
    )
    call_events.emit(
        "transfer.failed",
        room_name="cs-+886912",
        reason="sip_refer_error",
        workflow_run_id=7,
        transfer_reason="press0",
    )
    assert routed == [
        (
            "transfer.failed",
            {
                "room_name": "cs-+886912",
                "reason": "sip_refer_error",
                "workflow_run_id": 7,
                "elapsed_ms": None,
                "transfer_reason": "press0",
            },
        )
    ]


@pytest.mark.asyncio
async def test_immediate_event_sends_one_alert(monkeypatch, sent):
    monkeypatch.setenv("OBS_ALERT_WEBHOOK_URL", "https://hooks.example/x")
    alerts.notify(
        "transfer.failed", {"room_name": "cs-+886912", "reason": "sip_refer_error"}
    )
    await _drain()
    assert len(sent) == 1
    assert "transfer.failed" in sent[0]["text"]
    assert "cs-+886912" in sent[0]["text"]


@pytest.mark.asyncio
async def test_transfer_ok_not_alerted(monkeypatch, sent):
    monkeypatch.setenv("OBS_ALERT_WEBHOOK_URL", "https://hooks.example/x")
    alerts.notify("transfer.ok", {"room_name": "cs-+886912", "reason": "press0"})
    await _drain()
    assert sent == []


@pytest.mark.asyncio
async def test_webhook_unset_is_noop(sent):
    alerts.notify("transfer.failed", {"room_name": "cs-+886912", "reason": "x"})
    await _drain()
    assert sent == []


@pytest.mark.asyncio
async def test_send_failure_swallowed(monkeypatch):
    class _BoomClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            raise RuntimeError("webhook down")

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _BoomClient)
    await alerts._send("https://hooks.example/x", "text")  # must not raise


@pytest.mark.asyncio
async def test_provider_error_window_aggregates(monkeypatch, sent):
    monkeypatch.setenv("OBS_ALERT_WEBHOOK_URL", "https://hooks.example/x")
    monkeypatch.setenv("OBS_ERROR_THRESHOLD", "3")
    fake = _FakeRedis()
    alerts._redis = fake
    for _ in range(3):
        alerts.notify(
            "provider.error", {"room_name": "cs-+886912", "reason": "llm timeout"}
        )
        await _drain()
    assert len(sent) == 1
    assert "3 occurrences" in sent[0]["text"]
    assert fake.deletes == ["obs:alert:provider.error"]  # window reset after alert
    assert fake.expires[0][0] == "obs:alert:provider.error"
    # next error starts a fresh window, no immediate alert
    alerts.notify(
        "provider.error", {"room_name": "cs-+886912", "reason": "llm timeout"}
    )
    await _drain()
    assert len(sent) == 1


# --- safetynet log_event contract (1.1) ---


@pytest.mark.asyncio
async def test_safetynet_log_event_goes_through_emit(monkeypatch):
    from api.services.pipecat import livekit_safetynet as sn

    captured = []
    monkeypatch.setattr(
        call_events, "emit", lambda event, **fields: captured.append((event, fields))
    )
    sn.log_event(
        "safetynet.triggered",
        room_name="cs-+886912",
        reason="bot_silence",
        workflow_run_id=5,
    )
    assert captured[0][0] == "safetynet.triggered"
    assert captured[0][1]["room_name"] == "cs-+886912"
    assert captured[0][1]["workflow_run_id"] == 5


# --- watchdog provider.error emission (2.2) ---


def _push(frame):
    return types.SimpleNamespace(frame=frame)


@pytest.mark.asyncio
async def test_watchdog_emits_provider_error_once_per_frame(monkeypatch):
    from pipecat.frames.frames import ErrorFrame

    from api.services.pipecat.livekit_safetynet import SafetynetWatchdog

    captured = []
    monkeypatch.setattr(
        call_events, "emit", lambda event, **fields: captured.append((event, fields))
    )

    async def on_fatal(reason):
        pass

    wd = SafetynetWatchdog(
        on_fatal=on_fatal,
        threshold_seconds=8.0,
        poll_seconds=999,
        room_name="cs-+886912",
        workflow_run_id=9,
    )
    frame = ErrorFrame("llm timeout", fatal=False)
    await wd.on_push_frame(_push(frame))
    await wd.on_push_frame(_push(frame))  # same frame, second hop — deduped
    await wd.on_push_frame(_push(ErrorFrame("stt disconnect", fatal=False)))
    events = [e for e, _ in captured]
    assert events == ["provider.error", "provider.error"]
    assert captured[0][1]["room_name"] == "cs-+886912"
    assert captured[0][1]["workflow_run_id"] == 9
    await wd.stop()


@pytest.mark.asyncio
async def test_watchdog_fatal_error_not_counted_as_provider_error(monkeypatch):
    from pipecat.frames.frames import ErrorFrame

    from api.services.pipecat.livekit_safetynet import SafetynetWatchdog

    captured = []
    monkeypatch.setattr(
        call_events, "emit", lambda event, **fields: captured.append(event)
    )

    async def on_fatal(reason):
        pass

    wd = SafetynetWatchdog(on_fatal=on_fatal, threshold_seconds=8.0, poll_seconds=999)
    await wd.on_push_frame(_push(ErrorFrame("llm dead", fatal=True)))
    await _drain()
    assert "provider.error" not in captured
    await wd.stop()


# --- call outcome (3.x) ---


@pytest.fixture
def db_updates(monkeypatch):
    from api.db import db_client

    captured = []

    async def fake_update(run_id, **kwargs):
        captured.append({"run_id": run_id, **kwargs})

    monkeypatch.setattr(db_client, "update_workflow_run", fake_update)
    return captured


@pytest.mark.asyncio
async def test_outcome_written_to_annotations(db_updates):
    engine = types.SimpleNamespace()
    await record_call_outcome(
        engine, 7, outcome="transferred:press0", transfer_reason="press0"
    )
    assert db_updates == [
        {
            "run_id": 7,
            "annotations": {
                "call_outcome": "transferred:press0",
                "transfer_reason": "press0",
            },
        }
    ]
    assert engine._call_outcome == "transferred:press0"


@pytest.mark.asyncio
async def test_transfer_success_overwrites_earlier_failure(db_updates):
    engine = types.SimpleNamespace()
    await record_call_outcome(
        engine, 7, outcome="transfer_failed:press0", transfer_reason="press0"
    )
    await record_call_outcome(
        engine, 7, outcome="transferred:voice_tool", transfer_reason="voice_tool"
    )
    assert engine._call_outcome == "transferred:voice_tool"
    assert len(db_updates) == 2


@pytest.mark.asyncio
async def test_ai_completed_only_when_nothing_recorded(db_updates):
    engine = types.SimpleNamespace()
    await record_call_outcome(engine, 7, outcome="ai_completed")
    assert engine._call_outcome == "ai_completed"
    engine2 = types.SimpleNamespace()
    await record_call_outcome(
        engine2, 8, outcome="transferred:press0", transfer_reason="press0"
    )
    await record_call_outcome(engine2, 8, outcome="ai_completed")
    assert engine2._call_outcome == "transferred:press0"
    assert len(db_updates) == 2  # second ai_completed skipped


@pytest.mark.asyncio
async def test_outcome_never_raises(monkeypatch):
    from api.db import db_client

    async def broken_update(run_id, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(db_client, "update_workflow_run", broken_update)
    await record_call_outcome(types.SimpleNamespace(), 7, outcome="ai_completed")


@pytest.mark.asyncio
async def test_outcome_without_engine_or_run_id():
    await record_call_outcome(None, None, outcome="safetynet_terminated")


# --- transfer events from the shared funnel (2.1/2.3/4.1) ---


def _fake_engine():
    async def queue_frame(frame):
        pass

    async def end_call_with_reason(reason, abort_immediately=False):
        pass

    return types.SimpleNamespace(
        task=types.SimpleNamespace(queue_frame=queue_frame),
        end_call_with_reason=end_call_with_reason,
    )


def _fake_lk(*, raise_on_transfer=False):
    from api.services.pipecat.livekit_cold_transfer import SIP_KIND

    async def list_participants(req):
        return types.SimpleNamespace(
            participants=[types.SimpleNamespace(kind=SIP_KIND, identity="sip_caller")]
        )

    async def transfer(req):
        if raise_on_transfer:
            raise RuntimeError("provider rejected")

    return types.SimpleNamespace(
        room=types.SimpleNamespace(list_participants=list_participants),
        sip=types.SimpleNamespace(transfer_sip_participant=transfer),
    )


@pytest.mark.asyncio
async def test_refer_failure_emits_transfer_failed(monkeypatch, db_updates, sent):
    monkeypatch.setenv("OBS_ALERT_WEBHOOK_URL", "https://hooks.example/x")
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    emitted = []
    real_emit = call_events.emit

    def spy_emit(event, **fields):
        emitted.append((event, fields))
        real_emit(event, **fields)

    monkeypatch.setattr(call_events, "emit", spy_emit)

    res = await execute_cold_transfer(
        _fake_engine(),
        room_name="cs-+886912",
        destination="tel:+886900000000",
        schedule=None,
        transfer_reason="press0",
        lk=_fake_lk(raise_on_transfer=True),
    )
    assert res["status"] == "failed"
    assert emitted[0][0] == "transfer.failed"
    assert emitted[0][1]["transfer_reason"] == "press0"
    await _drain()
    assert len(sent) == 1  # one immediate alert reached the webhook (4.1)


@pytest.mark.asyncio
async def test_refer_success_emits_transfer_ok_and_outcome(monkeypatch, db_updates):
    from api.services.pipecat.livekit_transfer_flow import execute_cold_transfer

    emitted = []
    monkeypatch.setattr(
        call_events, "emit", lambda event, **fields: emitted.append((event, fields))
    )
    engine = _fake_engine()
    res = await execute_cold_transfer(
        engine,
        room_name="cs-+886912",
        destination="tel:+886900000000",
        schedule=None,
        transfer_reason="safetynet",
        lk=_fake_lk(),
    )
    assert res["status"] == "success"
    assert emitted[0][0] == "transfer.ok"
    assert engine._call_outcome == "transferred:safetynet"
