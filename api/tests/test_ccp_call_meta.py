"""ccp W4c caller metadata written at the LIVEKIT connect point."""

import types
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from livekit.protocol.models import ParticipantInfo
from loguru import logger
from sqlalchemy import select, text

from api.db.models import (
    CcpCallMetaModel,
    OrganizationModel,
    UserModel,
    WorkflowRunModel,
)
from api.services.ccp import call_meta
from api.services.ccp import caller_identity as ci

KEY = bytes(range(32))
STARTED = datetime(2026, 10, 6, 1, 2, 3, 456000, tzinfo=UTC)
SIP = ParticipantInfo.Kind.SIP
STANDARD = ParticipantInfo.Kind.STANDARD


def _p(kind, number=None):
    attrs = {} if number is None else {"sip.phoneNumber": number}
    return types.SimpleNamespace(kind=kind, attributes=attrs)


def _lk(*participants, error=None):
    async def list_participants(_req):
        if error:
            raise error
        return types.SimpleNamespace(participants=list(participants))

    return types.SimpleNamespace(room=types.SimpleNamespace(list_participants=list_participants))


@pytest.fixture
async def run_id(db_session, async_session):
    org = OrganizationModel(provider_id="test-org-call-meta")
    async_session.add(org)
    await async_session.flush()
    user = UserModel(provider_id="test-user-call-meta", selected_organization_id=org.id)
    async_session.add(user)
    await async_session.flush()
    wf = await db_session.create_workflow(
        name="Call Meta Workflow",
        workflow_definition={"nodes": [], "edges": []},
        user_id=user.id,
        organization_id=org.id,
    )
    run = WorkflowRunModel(name="call-meta-run", workflow_id=wf.id, mode="livekit")
    async_session.add(run)
    await async_session.flush()
    return run.id


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("CALLER_NUMBER_HMAC_KEY", KEY.hex())


@pytest.fixture
def logs():
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level="DEBUG")
    yield lines
    logger.remove(sink)


async def _meta(async_session, run_id):
    return (
        await async_session.execute(
            select(CcpCallMetaModel).where(CcpCallMetaModel.workflow_run_id == run_id)
        )
    ).scalar_one_or_none()


async def test_writes_mask_last4_hmac_and_audio_origin(
    run_id, key, async_session, logs
):
    lk = _lk(_p(STANDARD), _p(SIP, "0912345678"))
    await call_meta.record_call_meta(run_id, "cs-_x_y", STARTED, lk=lk)

    meta = await _meta(async_session, run_id)
    assert meta.caller_masked == "***5678"
    assert meta.caller_last4 == "5678"
    assert meta.caller_hmac == ci.caller_hmac(KEY, "+886912345678")
    assert meta.audio_started_at == STARTED
    assert not any("912345678" in line for line in logs)


async def test_anonymous_caller_writes_no_number(run_id, key, async_session):
    await call_meta.record_call_meta(run_id, "r", STARTED, lk=_lk(_p(SIP, "")))
    meta = await _meta(async_session, run_id)
    assert meta.caller_hmac is None and meta.caller_masked is None
    assert meta.audio_started_at == STARTED


async def test_shared_room_writes_no_number(run_id, key, async_session):
    lk = _lk(_p(SIP, "0912345678"), _p(SIP, "0922333444"))
    await call_meta.record_call_meta(run_id, "r", STARTED, lk=lk)
    meta = await _meta(async_session, run_id)
    assert meta.caller_hmac is None and meta.caller_last4 is None


async def test_missing_key_writes_no_number(run_id, monkeypatch, async_session):
    monkeypatch.delenv("CALLER_NUMBER_HMAC_KEY", raising=False)
    await call_meta.record_call_meta(run_id, "r", STARTED, lk=_lk(_p(SIP, "0912345678")))
    meta = await _meta(async_session, run_id)
    assert meta.caller_last4 is None and meta.audio_started_at == STARTED


async def test_livekit_failure_still_records_origin(run_id, key, async_session, logs):
    lk = _lk(error=RuntimeError("lookup for sip_+886912345678 failed"))
    await call_meta.record_call_meta(run_id, "r", STARTED, lk=lk)
    meta = await _meta(async_session, run_id)
    assert meta.audio_started_at == STARTED and meta.caller_hmac is None
    assert not any("912345678" in line for line in logs)


async def test_db_failure_does_not_raise(key, logs):
    # No such run: the FK rejects the insert; the hook swallows it.
    await call_meta.record_call_meta(
        2_000_000_000, "r", STARTED, lk=_lk(_p(SIP, "0912345678"))
    )
    assert not any("912345678" in line for line in logs)


async def test_first_write_wins(run_id, key, async_session):
    await call_meta.record_call_meta(run_id, "r", STARTED, lk=_lk(_p(SIP, "0912345678")))
    later = datetime(2026, 10, 6, 2, tzinfo=UTC)
    await call_meta.record_call_meta(run_id, "r", later, lk=_lk(_p(SIP, "0922333444")))
    meta = await _meta(async_session, run_id)
    assert meta.caller_last4 == "5678" and meta.audio_started_at == STARTED


async def test_key_change_is_flagged_but_still_written(run_id, key, async_session, logs):
    await async_session.execute(
        text("INSERT INTO ccp_settings (id, key_fingerprint) VALUES (1, 'deadbeef')")
    )
    await call_meta.record_call_meta(run_id, "r", STARTED, lk=_lk(_p(SIP, "0912345678")))
    assert (await _meta(async_session, run_id)).caller_last4 == "5678"
    assert any("CALLER_NUMBER_HMAC_KEY changed" in line for line in logs)


async def test_fingerprint_recorded_on_first_use(run_id, key, async_session):
    await call_meta.record_call_meta(run_id, "r", STARTED, lk=_lk(_p(SIP, "0912345678")))
    stored = (
        await async_session.execute(text("SELECT key_fingerprint FROM ccp_settings"))
    ).scalar_one()
    assert stored == ci.key_fingerprint(KEY)


# --- the connect-point hook ---


def _register(consent_gate):
    from api.services.pipecat.event_handlers import register_event_handlers

    handlers = {}
    transport = types.SimpleNamespace(
        add_event_handler=lambda name, fn: handlers.__setitem__(name, fn)
    )
    task = types.SimpleNamespace(
        event_handler=lambda name: (lambda fn: fn), turn_trace_observer=None
    )
    audio_buffer = MagicMock()
    audio_buffer.start_recording = AsyncMock()
    register_event_handlers(
        task,
        transport,
        workflow_run_id=7,
        engine=MagicMock(),
        audio_buffer=audio_buffer,
        in_memory_logs_buffer=MagicMock(),
        pipeline_metrics_aggregator=MagicMock(),
        audio_config=None,
        consent_gate=consent_gate,
    )
    return handlers["on_client_connected"]


async def test_hook_runs_for_livekit_inbound_only():
    gate = types.SimpleNamespace(room_name="cs-_x_y")
    with patch.object(call_meta, "on_connected") as hook:
        await _register(gate)()
        await _register(None)()
    assert hook.call_count == 1
    run_id, room, started = hook.call_args.args
    assert (run_id, room) == (7, "cs-_x_y") and started.tzinfo is not None
