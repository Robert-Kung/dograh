"""ccp W4c split retention — candidate queries and clears against the real DB."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from api.db.models import (
    CcpCallMetaModel,
    OrganizationModel,
    UserModel,
    WorkflowRunModel,
)

STARTED = datetime(2026, 1, 1, tzinfo=UTC)
GATHERED = {
    "extracted_variables": {"customer_name": "王小明"},
    "customer_name": "王小明",  # extraction is also copied to the top level
    "call_disposition": "billing_inquiry",
    "mapped_call_disposition": "billing_inquiry",
    "call_tags": ["user_speech", "billing_inquiry"],
    "nodes_visited": ["start", "end"],
    "trace_url": "https://trace.example/1",
}
LOGS = {
    "realtime_feedback_events": [{"type": "rtf-bot-text", "payload": {"text": "hi"}}],
    "tuner_payload": {"x": 1},
}


@pytest.fixture
async def make_run(db_session, async_session):
    org = OrganizationModel(provider_id="test-org-retention-split")
    async_session.add(org)
    await async_session.flush()
    user = UserModel(
        provider_id="test-user-retention-split", selected_organization_id=org.id
    )
    async_session.add(user)
    await async_session.flush()
    wf = await db_session.create_workflow(
        name="Retention Split Workflow",
        workflow_definition={"nodes": [], "edges": []},
        user_id=user.id,
        organization_id=org.id,
    )

    async def _make(age_days: int, *, mode="livekit", meta=True, **cols):
        run = WorkflowRunModel(
            name="retention-split-run",
            workflow_id=wf.id,
            mode=mode,
            created_at=datetime.now(UTC) - timedelta(days=age_days),
            **cols,
        )
        async_session.add(run)
        await async_session.flush()
        if meta:
            async_session.add(
                CcpCallMetaModel(
                    workflow_run_id=run.id,
                    caller_masked="***5678",
                    caller_last4="5678",
                    caller_hmac="a" * 64,
                    audio_started_at=STARTED,
                )
            )
            await async_session.flush()
        return run.id

    return _make


async def _ids(coro) -> set[int]:
    return {r.id for r in await coro}


async def test_audio_candidates(make_run, db_session):
    old = await make_run(181, recording_url="recordings/a.wav", meta=False)
    tracks = await make_run(
        181, extra={"recordings": {"user": "recordings/u.wav"}}, meta=False
    )
    fresh = await make_run(179, recording_url="recordings/b.wav", meta=False)
    transcript_only = await make_run(
        181, transcript_url="transcripts/c.txt", meta=False
    )

    picked = await _ids(db_session.get_expired_audio_runs(180, limit=10_000))
    assert {old, tracks} <= picked
    assert not {fresh, transcript_only} & picked

    await db_session.clear_audio_artifacts(old)
    await db_session.clear_audio_artifacts(tracks)
    picked = await _ids(db_session.get_expired_audio_runs(180, limit=10_000))
    assert not {old, tracks} & picked  # idempotent


async def test_transcript_expiry_clears_every_copy(make_run, db_session, async_session):
    run_id = await make_run(
        31,
        transcript_url="transcripts/1.txt",
        recording_url="recordings/1.wav",
        logs=dict(LOGS),
        gathered_context=dict(GATHERED),
    )
    fresh = await make_run(
        29, transcript_url="t/2.txt", gathered_context=dict(GATHERED)
    )
    editor = await make_run(31, mode="smallwebrtc", meta=False, logs=dict(LOGS))
    clean = await make_run(
        31, meta=False, gathered_context={"mapped_call_disposition": "x"}
    )

    picked = await _ids(db_session.get_expired_transcript_runs(30, limit=10_000))
    assert {run_id, editor} <= picked
    assert not {fresh, clean} & picked

    await db_session.clear_transcript_artifacts(run_id)

    run = (
        await async_session.execute(
            select(WorkflowRunModel).where(WorkflowRunModel.id == run_id)
        )
    ).scalar_one()
    await async_session.refresh(run)
    assert run.transcript_url is None
    assert run.recording_url == "recordings/1.wav"  # audio has its own setting
    assert run.logs == {"tuner_payload": {"x": 1}}
    assert run.gathered_context == {
        "mapped_call_disposition": "billing_inquiry",
        "nodes_visited": ["start", "end"],
        "trace_url": "https://trace.example/1",
    }
    meta = (
        await async_session.execute(
            select(CcpCallMetaModel).where(CcpCallMetaModel.workflow_run_id == run_id)
        )
    ).scalar_one()
    await async_session.refresh(meta)
    assert (meta.caller_masked, meta.caller_last4, meta.caller_hmac) == (None,) * 3
    assert meta.audio_started_at == STARTED

    picked = await _ids(db_session.get_expired_transcript_runs(30, limit=10_000))
    assert run_id not in picked  # idempotent


async def test_number_only_run_is_a_transcript_candidate(make_run, db_session):
    run_id = await make_run(31)
    assert run_id in await _ids(
        db_session.get_expired_transcript_runs(30, limit=10_000)
    )


async def test_audit_scope_is_written(make_run, db_session, async_session):
    from api.db.models import RecordingRetentionAuditModel

    run_id = await make_run(31, meta=False)
    await db_session.create_recording_retention_audit(
        run_id, object_keys=[], retention_days=30, result="ok", scope="transcript"
    )
    row = (
        await async_session.execute(
            select(RecordingRetentionAuditModel).where(
                RecordingRetentionAuditModel.workflow_run_id == run_id
            )
        )
    ).scalar_one()
    assert row.scope == "transcript"
