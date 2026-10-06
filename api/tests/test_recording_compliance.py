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
    monkeypatch.delenv("RECORD_AUDIO_RETENTION_DAYS", raising=False)
    validate_recording_config()
    assert lc.audio_retention_days() == 180


def test_validate_config_bad_days_fails(monkeypatch):
    monkeypatch.setenv("RECORD_AUDIO_RETENTION_DAYS", "soon")
    with pytest.raises(RuntimeError, match="RECORD_AUDIO_RETENTION_DAYS"):
        validate_recording_config()


def test_validate_config_nonpositive_days_fails(monkeypatch):
    monkeypatch.setenv("RECORD_AUDIO_RETENTION_DAYS", "0")
    with pytest.raises(RuntimeError, match="must be > 0"):
        validate_recording_config()


def test_old_key_is_no_longer_read(monkeypatch):
    monkeypatch.delenv("RECORD_AUDIO_RETENTION_DAYS", raising=False)
    monkeypatch.setenv("RECORD_RETENTION_DAYS", "7")
    assert lc.audio_retention_days() == 180


@pytest.mark.parametrize(
    "value,expected",
    [
        ("30", 30),
        (" 365 ", 365),
        ("never", "never"),
        ("0", None),
        ("", None),
        ("-5", None),
        ("thirty", None),
        ("３０", None),
        ("Never", None),
    ],
)
def test_transcript_retention_parsing(monkeypatch, value, expected):
    monkeypatch.setenv("RECORD_TRANSCRIPT_RETENTION_DAYS", value)
    assert lc.transcript_retention() == expected


def test_transcript_retention_unset_does_not_fail_startup(monkeypatch):
    monkeypatch.delenv("RECORD_TRANSCRIPT_RETENTION_DAYS", raising=False)
    assert lc.transcript_retention() is None
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
    # ccp W4c: the run says "recording was off", not just "no consent record"
    assert db_updates == [
        {"run_id": 7, "annotations": {"consent_notice": {"disabled": True}}}
    ]


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
    assert consent_events[0][1]["reason"] == "playback_error"
    record = db_updates[0]["annotations"]["consent_notice"]
    assert record["failed_reason"] == "playback_error" and record["failed_at"]
    assert "tts dead" not in str(db_updates) + str(consent_events)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("notice", [None, "本通話將錄音。"])
async def test_failure_and_disabled_persist_failures_swallowed(
    monkeypatch, consent_events, notice
):
    from api.db import db_client

    if notice:
        monkeypatch.setenv("RECORD_CONSENT_NOTICE_TEXT", notice)
    else:
        monkeypatch.delenv("RECORD_CONSENT_NOTICE_TEXT", raising=False)

    async def broken_update(run_id, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(db_client, "update_workflow_run", broken_update)
    engine, _ = _fake_engine(queue_raises=True)
    gate = RecordingConsentGate(engine, room_name="cs-+886912", workflow_run_id=7)
    await gate.play_notice()  # must not raise (C4)
    assert not gate.should_record


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
    monkeypatch.setenv("RECORD_AUDIO_RETENTION_DAYS", "180")
    monkeypatch.setenv("RECORD_TRANSCRIPT_RETENTION_DAYS", "30")

    from api.db import db_client
    from api.tasks import recording_retention as rr

    state = {
        "runs": [],
        "transcript_runs": [],
        "queried": {},
        "cleared": [],
        "transcript_cleared": [],
        "audits": [],
        "fs": _FakeFS(),
    }

    async def fake_expired_audio(days, limit=500):
        state["queried"]["audio"] = days
        return state["runs"]

    async def fake_expired_transcript(days, limit=500):
        state["queried"]["transcript"] = days
        return state["transcript_runs"]

    async def fake_clear(run_id):
        state["cleared"].append(run_id)

    async def fake_clear_transcript(run_id):
        state["transcript_cleared"].append(run_id)

    async def fake_audit(run_id, *, object_keys, retention_days, result, scope):
        state["audits"].append(
            {
                "run_id": run_id,
                "object_keys": object_keys,
                "retention_days": retention_days,
                "result": result,
                "scope": scope,
            }
        )

    monkeypatch.setattr(db_client, "get_expired_audio_runs", fake_expired_audio)
    monkeypatch.setattr(
        db_client, "get_expired_transcript_runs", fake_expired_transcript
    )
    monkeypatch.setattr(db_client, "clear_audio_artifacts", fake_clear)
    monkeypatch.setattr(
        db_client, "clear_transcript_artifacts", fake_clear_transcript
    )
    monkeypatch.setattr(db_client, "create_recording_retention_audit", fake_audit)
    monkeypatch.setattr(rr, "get_storage_for_backend", lambda backend: state["fs"])
    state["events"] = []
    monkeypatch.setattr(
        rr,
        "log_retention_event",
        lambda run_id, keys, days, scope: state["events"].append((run_id, scope)),
    )
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
    # The audio sweep leaves the transcript to its own setting.
    assert set(retention_env["fs"].deleted) == {
        "recordings/1.wav",
        "recordings/1/user.wav",
        "recordings/1/bot.wav",
    }
    assert retention_env["cleared"] == [1]
    audit = retention_env["audits"][0]
    assert audit["result"] == "ok" and audit["scope"] == "audio"
    assert audit["retention_days"] == 180
    assert retention_env["events"] == [(1, "audio")]


@pytest.mark.asyncio
async def test_retention_single_failure_continues_batch(retention_env):
    from api.tasks.recording_retention import enforce_recording_retention

    retention_env["fs"] = _FakeFS(fail_keys={"recordings/1.wav"})
    retention_env["runs"] = [_run(1), _run(2)]
    await enforce_recording_retention(None)
    assert retention_env["cleared"] == [2]  # run 1 left for the next sweep
    assert retention_env["events"] == [(2, "audio")]
    results = {a["run_id"]: a["result"] for a in retention_env["audits"]}
    assert results[1].startswith("failed")
    assert results[2] == "ok"


@pytest.mark.asyncio
async def test_transcript_sweep_uses_its_own_setting(retention_env):
    from api.tasks.recording_retention import enforce_recording_retention

    retention_env["transcript_runs"] = [
        _run(3, recording=None, transcript="transcripts/3.txt"),
        _run(4, recording=None),  # DB copy / number / extracted values only
    ]
    await enforce_recording_retention(None)
    assert retention_env["queried"] == {"audio": 180, "transcript": 30}
    assert retention_env["fs"].deleted == ["transcripts/3.txt"]
    assert retention_env["transcript_cleared"] == [3, 4]
    assert {(a["run_id"], a["scope"], a["retention_days"]) for a in retention_env["audits"]} == {
        (3, "transcript", 30),
        (4, "transcript", 30),
    }


@pytest.mark.asyncio
async def test_transcript_never_skips_the_sweep(retention_env, monkeypatch):
    from api.tasks.recording_retention import enforce_recording_retention

    monkeypatch.setenv("RECORD_TRANSCRIPT_RETENTION_DAYS", "never")
    retention_env["transcript_runs"] = [_run(3, recording=None, transcript="t/3.txt")]
    await enforce_recording_retention(None)
    assert "transcript" not in retention_env["queried"]
    assert retention_env["transcript_cleared"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "0", "soon"])
async def test_transcript_setting_missing_skips_and_logs(retention_env, monkeypatch, value):
    from loguru import logger

    from api.tasks.recording_retention import enforce_recording_retention

    if value is None:
        monkeypatch.delenv("RECORD_TRANSCRIPT_RETENTION_DAYS", raising=False)
    else:
        monkeypatch.setenv("RECORD_TRANSCRIPT_RETENTION_DAYS", value)
    lines = []
    sink = logger.add(lambda m: lines.append(str(m)), level="ERROR")
    try:
        await enforce_recording_retention(None)
    finally:
        logger.remove(sink)
    assert "transcript" not in retention_env["queried"]
    assert retention_env["queried"]["audio"] == 180  # audio still swept
    assert any("RECORD_TRANSCRIPT_RETENTION_DAYS" in line for line in lines)


@pytest.mark.asyncio
async def test_retention_noop_when_nothing_expired(retention_env):
    from api.tasks.recording_retention import enforce_recording_retention

    retention_env["runs"] = []
    await enforce_recording_retention(None)
    assert retention_env["audits"] == []


@pytest.mark.asyncio
async def test_realtime_pipeline_no_notice_no_recording(
    monkeypatch, consent_events, db_updates
):
    """Speech-to-speech has no TTS service — a queued notice would be silently
    dropped; consent must not be recorded (H1)."""
    monkeypatch.setenv("RECORD_CONSENT_NOTICE_TEXT", "本通話將錄音。")
    engine, frames = _fake_engine()
    gate = RecordingConsentGate(
        engine, room_name="cs-+886912", workflow_run_id=7, is_realtime=True
    )
    await gate.play_notice()
    assert not gate.should_record
    assert frames == []
    assert consent_events[0][0] == "consent.notice_failed"
    assert consent_events[0][1]["reason"] == "realtime_no_tts_path"
    record = db_updates[0]["annotations"]["consent_notice"]
    assert record["failed_reason"] == "realtime_no_tts_path"


@pytest.mark.asyncio
async def test_audio_greeting_no_notice_no_recording(
    monkeypatch, consent_events, db_updates
):
    """Audio greetings bypass the pipeline FIFO — notice-first can't be
    guaranteed, so fail-safe applies (H2)."""
    monkeypatch.setenv("RECORD_CONSENT_NOTICE_TEXT", "本通話將錄音。")
    engine, frames = _fake_engine()
    engine.workflow = types.SimpleNamespace(start_node_id="n1")
    engine.get_node_greeting = lambda node_id: ("audio", "42")
    gate = RecordingConsentGate(engine, room_name="cs-+886912", workflow_run_id=7)
    await gate.play_notice()
    assert not gate.should_record
    assert frames == []
    assert consent_events[0][1]["reason"] == "audio_greeting_ordering_unsupported"
    record = db_updates[0]["annotations"]["consent_notice"]
    assert record["failed_reason"] == "audio_greeting_ordering_unsupported"


@pytest.mark.asyncio
async def test_text_greeting_still_records(monkeypatch, consent_events, db_updates):
    monkeypatch.setenv("RECORD_CONSENT_NOTICE_TEXT", "本通話將錄音。")
    engine, frames = _fake_engine()
    engine.workflow = types.SimpleNamespace(start_node_id="n1")
    engine.get_node_greeting = lambda node_id: ("text", "您好")
    gate = RecordingConsentGate(engine, room_name="cs-+886912", workflow_run_id=7)
    await gate.play_notice()
    assert gate.should_record
    assert len(frames) == 1
