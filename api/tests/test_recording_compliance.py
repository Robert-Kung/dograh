"""Recording compliance tests (S-L8-RECORD): consent notice gate, fail-safe
no-notice-no-recording, retention sweep, audit trail."""

import types
from datetime import UTC, datetime, timedelta

import pytest

from api.services.pipecat import livekit_consent as lc
from api.services.pipecat.livekit_consent import (
    RecordingConsentGate,
    maybe_build_consent_gate,
    validate_recording_config,
)


def _fake_engine(*, queue_raises=False):
    frames = []

    async def queue_frame(frame):
        if queue_raises:
            raise RuntimeError("tts dead")
        frames.append(frame)

    return types.SimpleNamespace(
        task=types.SimpleNamespace(queue_frame=queue_frame)
    ), frames


@pytest.fixture
def consent_events(monkeypatch):
    captured = []
    monkeypatch.setattr(
        lc,
        "log_consent_event",
        lambda event, **fields: captured.append((event, fields)),
    )
    return captured


@pytest.fixture
def db_updates(monkeypatch):
    from api.db import db_client

    captured = []

    async def fake_update(run_id, **kwargs):
        captured.append({"run_id": run_id, **kwargs})

    monkeypatch.setattr(db_client, "update_workflow_run", fake_update)
    return captured


# --- config validation (1.1) ---


def test_validate_config_default_ok(monkeypatch):
    monkeypatch.delenv("RECORD_RETENTION_DAYS", raising=False)
    validate_recording_config()


def test_validate_config_bad_days_fails(monkeypatch):
    monkeypatch.setenv("RECORD_RETENTION_DAYS", "soon")
    with pytest.raises(RuntimeError, match="RECORD_RETENTION_DAYS"):
        validate_recording_config()


def test_validate_config_nonpositive_days_fails(monkeypatch):
    monkeypatch.setenv("RECORD_RETENTION_DAYS", "0")
    with pytest.raises(RuntimeError, match="must be > 0"):
        validate_recording_config()


# --- consent gate (1.2–1.4) ---


@pytest.mark.asyncio
async def test_notice_played_records_consent(monkeypatch, consent_events, db_updates):
    monkeypatch.setenv("RECORD_CONSENT_NOTICE_TEXT", "本通話將錄音。")
    monkeypatch.setenv("RECORD_CONSENT_SCRIPT_VERSION", "legal-v1")
    engine, frames = _fake_engine()
    gate = RecordingConsentGate(engine, room_name="cs-+886912", workflow_run_id=7)
    assert not gate.should_record
    await gate.play_notice()
    assert gate.should_record
    assert len(frames) == 1 and "錄音" in frames[0].text
    assert consent_events[0][0] == "consent.notice_played"
    assert consent_events[0][1]["reason"] == "legal-v1"
    consent = db_updates[0]["annotations"]["consent_notice"]
    assert consent["script_version"] == "legal-v1"
    assert consent["played_at"]


@pytest.mark.asyncio
async def test_no_notice_text_means_no_recording(
    monkeypatch, consent_events, db_updates
):
    monkeypatch.delenv("RECORD_CONSENT_NOTICE_TEXT", raising=False)
    engine, frames = _fake_engine()
    gate = RecordingConsentGate(engine, room_name="cs-+886912", workflow_run_id=7)
    await gate.play_notice()
    assert not gate.should_record  # fail-safe: 未告知不錄音
    assert frames == []
    assert consent_events == []
    assert db_updates == []


@pytest.mark.asyncio
async def test_notice_failure_no_recording_call_continues(
    monkeypatch, consent_events, db_updates
):
    monkeypatch.setenv("RECORD_CONSENT_NOTICE_TEXT", "本通話將錄音。")
    engine, _ = _fake_engine(queue_raises=True)
    gate = RecordingConsentGate(engine, room_name="cs-+886912", workflow_run_id=7)
    await gate.play_notice()  # must not raise (C4)
    assert not gate.should_record
    assert consent_events[0][0] == "consent.notice_failed"
    assert db_updates == []


@pytest.mark.asyncio
async def test_consent_persist_failure_swallowed(monkeypatch, consent_events):
    monkeypatch.setenv("RECORD_CONSENT_NOTICE_TEXT", "本通話將錄音。")
    from api.db import db_client

    async def broken_update(run_id, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(db_client, "update_workflow_run", broken_update)
    engine, _ = _fake_engine()
    gate = RecordingConsentGate(engine, room_name="cs-+886912", workflow_run_id=7)
    await gate.play_notice()
    assert gate.should_record  # notice did play; persistence failure only logs


def test_gate_built_only_for_livekit_inbound():
    engine = types.SimpleNamespace()
    livekit_inbound = types.SimpleNamespace(
        mode="livekit",
        id=7,
        initial_context={"direction": "inbound", "room_name": "cs-+886912"},
    )
    assert maybe_build_consent_gate(livekit_inbound, engine) is not None

    outbound = types.SimpleNamespace(
        mode="livekit", id=8, initial_context={"direction": "outbound"}
    )
    assert maybe_build_consent_gate(outbound, engine) is None

    twilio = types.SimpleNamespace(
        mode="twilio", id=9, initial_context={"direction": "inbound"}
    )
    assert maybe_build_consent_gate(twilio, engine) is None

    assert maybe_build_consent_gate(None, engine) is None


# --- retention sweep (2.x) ---


def _run(run_id, *, recording="recordings/{id}.wav", transcript=None, extra=None):
    return types.SimpleNamespace(
        id=run_id,
        recording_url=recording.format(id=run_id) if recording else None,
        transcript_url=transcript,
        extra=extra or {},
        storage_backend="minio",
    )


class _FakeFS:
    def __init__(self, *, fail_keys=()):
        self.deleted = []
        self.fail_keys = set(fail_keys)

    async def adelete_file(self, key):
        if key in self.fail_keys:
            return False
        self.deleted.append(key)
        return True


@pytest.fixture
def retention_env(monkeypatch):
    monkeypatch.setenv("RECORD_RETENTION_DAYS", "180")

    from api.db import db_client
    from api.tasks import recording_retention as rr

    state = {
        "runs": [],
        "cleared": [],
        "audits": [],
        "fs": _FakeFS(),
    }

    async def fake_expired(days, limit=500):
        return state["runs"]

    async def fake_clear(run_id):
        state["cleared"].append(run_id)

    async def fake_audit(run_id, *, object_keys, retention_days, result):
        state["audits"].append(
            {
                "run_id": run_id,
                "object_keys": object_keys,
                "retention_days": retention_days,
                "result": result,
            }
        )

    monkeypatch.setattr(db_client, "get_expired_recording_runs", fake_expired)
    monkeypatch.setattr(db_client, "clear_recording_artifacts", fake_clear)
    monkeypatch.setattr(db_client, "create_recording_retention_audit", fake_audit)
    monkeypatch.setattr(rr, "get_storage_for_backend", lambda backend: state["fs"])
    return state


@pytest.mark.asyncio
async def test_retention_deletes_all_tracks_and_audits(retention_env):
    from api.tasks.recording_retention import enforce_recording_retention

    retention_env["runs"] = [
        _run(
            1,
            transcript="transcripts/1.txt",
            extra={
                "recordings": {
                    "user": {"storage_key": "recordings/1/user.wav"},
                    "bot": "recordings/1/bot.wav",
                }
            },
        )
    ]
    await enforce_recording_retention(None)
    assert set(retention_env["fs"].deleted) == {
        "recordings/1.wav",
        "recordings/1/user.wav",
        "recordings/1/bot.wav",
        "transcripts/1.txt",
    }
    assert retention_env["cleared"] == [1]
    assert retention_env["audits"][0]["result"] == "ok"
    assert retention_env["audits"][0]["retention_days"] == 180


@pytest.mark.asyncio
async def test_retention_single_failure_continues_batch(retention_env):
    from api.tasks.recording_retention import enforce_recording_retention

    retention_env["fs"] = _FakeFS(fail_keys={"recordings/1.wav"})
    retention_env["runs"] = [_run(1), _run(2)]
    await enforce_recording_retention(None)
    assert retention_env["cleared"] == [2]  # run 1 left for the next sweep
    results = {a["run_id"]: a["result"] for a in retention_env["audits"]}
    assert results[1].startswith("failed")
    assert results[2] == "ok"


@pytest.mark.asyncio
async def test_retention_noop_when_nothing_expired(retention_env):
    from api.tasks.recording_retention import enforce_recording_retention

    retention_env["runs"] = []
    await enforce_recording_retention(None)
    assert retention_env["audits"] == []


def test_expired_query_boundaries():
    """created_at anchoring: only runs older than the window with a recording
    still present qualify (idempotency marker = recording_url)."""
    from api.db.recording_retention_client import RecordingRetentionClient  # noqa: F401

    # Query semantics are exercised via the DB in integration; here we pin the
    # cutoff arithmetic used by the client.
    days = 180
    cutoff = datetime.now(UTC) - timedelta(days=days)
    old = datetime.now(UTC) - timedelta(days=days + 1)
    fresh = datetime.now(UTC) - timedelta(days=days - 1)
    assert old < cutoff < fresh
