"""Answering participant tests (answer-before-refer §2): prompt validation,
token grants, the bounded join/publish/play lifecycle, events and alert routing.

rtc is faked at the ``livekit.rtc`` attributes ``answering`` reads; the real
join → 200 OK → REFER sequence is covered by the harness (task 0, AC1–AC5).
"""

import asyncio
import struct
import time
import types
import wave

import pytest

from api.services.observability import alerts, call_events
from api.services.pipecat import livekit_answer as la
from api.services.pipecat.livekit_answer import AnswerFailed, answering

ROOM = "cs-_+886212345678_abc"


@pytest.fixture
def events(monkeypatch):
    captured = []

    def fake_emit(event, **fields):
        captured.append({"event": event, **fields})

    monkeypatch.setattr(call_events, "emit", fake_emit)
    return captured


@pytest.fixture
def prompts(monkeypatch):
    """Two short prompts (50 ms each, after the 300 ms lead) and answering on."""
    pcm = b"\x01\x00" * la.FRAME_SAMPLES * 3
    monkeypatch.setattr(la, "_prompts", {"transfer": pcm, "end": pcm})
    monkeypatch.setattr(la, "_disabled_reason", None)
    monkeypatch.setattr(la, "_active", 0)
    monkeypatch.setenv("LIVEKIT_URL", "ws://livekit-server:7880")
    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secret-secret-secret-secret-secret")
    return pcm


# --- fake rtc -----------------------------------------------------------------


class FakeRtc:
    """Stand-ins for the rtc names ``answering`` uses, with failure knobs."""

    def __init__(self):
        self.rooms = []
        self.sources = []
        self.built_in_loop = []
        self.connect_hang = False
        self.connect_error = None
        self.publish_error = None
        self.disconnect_hang = False
        self.capture_hang = False
        self.capture_error = None
        self.options = None
        fake = self

        class Room:
            def __init__(self):
                fake.built_in_loop.append(_has_running_loop())
                self.handlers = {}
                self.disconnected = False
                self.published = []
                self.local_participant = types.SimpleNamespace(
                    publish_track=self._publish
                )
                fake.rooms.append(self)

            async def connect(self, url, token, options=None):
                fake.options = options
                self.token = token
                if fake.connect_error:
                    raise fake.connect_error
                if fake.connect_hang:
                    await asyncio.Event().wait()

            async def _publish(self, track, opts):
                if fake.publish_error:
                    raise fake.publish_error
                self.published.append((track, opts))

            def on(self, event, cb):
                self.handlers[event] = cb

            async def disconnect(self):
                if fake.disconnect_hang:
                    await asyncio.Event().wait()
                self.disconnected = True

        class AudioSource:
            def __init__(self, rate, channels, queue_size_ms=1000):
                fake.built_in_loop.append(_has_running_loop())
                self.frames = 0
                self.playout_waited = False
                self.closed = False
                fake.sources.append(self)

            async def capture_frame(self, frame):
                if fake.capture_error:
                    raise fake.capture_error
                if fake.capture_hang:
                    await asyncio.Event().wait()
                self.frames += 1

            async def wait_for_playout(self):
                self.playout_waited = True

            async def aclose(self):
                self.closed = True

        self.Room = Room
        self.AudioSource = AudioSource
        self.LocalAudioTrack = types.SimpleNamespace(
            create_audio_track=lambda name, source: ("track", name)
        )
        self.RoomOptions = lambda **kw: types.SimpleNamespace(**kw)
        self.TrackPublishOptions = lambda **kw: types.SimpleNamespace(**kw)
        self.TrackSource = types.SimpleNamespace(SOURCE_MICROPHONE=2)
        self.ParticipantKind = types.SimpleNamespace(PARTICIPANT_KIND_SIP=3)
        self.AudioFrame = lambda data, rate, ch, n: (len(data), rate, ch, n)


def _has_running_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


@pytest.fixture
def rtc(monkeypatch):
    from livekit import rtc as real

    fake = FakeRtc()
    for name in (
        "Room",
        "AudioSource",
        "LocalAudioTrack",
        "RoomOptions",
        "TrackPublishOptions",
        "TrackSource",
        "ParticipantKind",
        "AudioFrame",
    ):
        monkeypatch.setattr(real, name, getattr(fake, name))
    return fake


# --- 2.1 prompt validation ------------------------------------------------------


def _wav(path, *, rate=48000, channels=1, width=2, frames=4800):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(
            struct.pack(f"<{frames * channels}h", *([100] * frames * channels))
            if width == 2
            else b"\x80" * frames * channels
        )


@pytest.fixture
def asset_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", "ws://x")
    monkeypatch.setenv("ANSWER_AUDIO_DIR", str(tmp_path))
    monkeypatch.setattr(la, "_prompts", {})
    monkeypatch.setattr(la, "_disabled_reason", "not_validated")
    for f in la.PROMPT_FILES.values():
        _wav(tmp_path / f)
    return tmp_path


def test_validate_ok_caches_pcm_with_lead_silence(asset_dir, events):
    la.validate_answer_assets()
    assert la.disabled_reason() is None
    lead = int(la.LEAD_SILENCE_SECONDS * la.SAMPLE_RATE) * la.SAMPLE_WIDTH
    for pcm in la._prompts.values():
        assert pcm[:lead] == b"\x00" * lead
        assert len(pcm) % la.FRAME_BYTES == 0
        assert la._prompt_seconds(pcm) == pytest.approx(0.4, abs=0.021)
    # D5: connect + publish + one play of each prompt + disconnect
    assert la.hard_cap_seconds() == pytest.approx(
        20 + 2 + 2 * (0.4 + 1.0) + 2, abs=0.05
    )
    assert events == []


async def test_validate_missing_file_disables_without_raising(asset_dir, events):
    (asset_dir / "answer_end.wav").unlink()
    la.validate_answer_assets()  # never raises: boot continues (AC12)
    assert "answer_end.wav" in la.disabled_reason()
    assert [e["event"] for e in events] == ["answer.disabled"]
    with pytest.raises(AnswerFailed) as ei:
        async with answering(ROOM, reason="no_did"):
            pass
    assert ei.value.stage == "disabled"


@pytest.mark.parametrize(
    "kw", [{"rate": 16000}, {"channels": 2}, {"width": 1}, {"frames": 0}]
)
def test_validate_bad_format_disables(asset_dir, events, kw):
    _wav(asset_dir / "answer_transfer.wav", **kw)
    la.validate_answer_assets()
    assert "answer_transfer.wav" in la.disabled_reason()
    assert events[0]["event"] == "answer.disabled"
    assert events[0]["room_name"] == ""


def test_validate_skipped_without_livekit(asset_dir, events, monkeypatch):
    monkeypatch.delenv("LIVEKIT_URL")
    (asset_dir / "answer_end.wav").unlink()
    la.validate_answer_assets()
    assert la.disabled_reason() == "not_validated"
    assert events == []


# --- 2.2 token grants -------------------------------------------------------------


def _claims(jwt: str):
    from livekit import api

    return api.TokenVerifier("key", "secret-secret-secret-secret-secret").verify(jwt)


def test_agent_token_unchanged_and_answer_token_narrowed(monkeypatch):
    from api.services.pipecat.livekit_dispatcher import _sign_agent_token

    monkeypatch.setenv("LIVEKIT_API_KEY", "key")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secret-secret-secret-secret-secret")

    agent = _claims(_sign_agent_token(ROOM, "agent-7")).video
    assert agent.room_join and agent.room == ROOM
    assert agent.can_publish and agent.can_subscribe
    assert agent.can_publish_data is True  # the grant's default, as before
    assert not agent.can_publish_sources

    claims = _claims(
        _sign_agent_token(
            ROOM,
            "agent-answer-ab12cd34",
            can_subscribe=False,
            can_publish_data=False,
            can_publish_sources=["microphone"],
            ttl_seconds=60,
        )
    )
    v = claims.video
    assert v.can_publish and not v.can_subscribe
    assert v.can_publish_data is False
    assert v.can_publish_sources == ["microphone"]
    assert claims.identity == "agent-answer-ab12cd34"


# --- 2.3 / 2.5 lifecycle --------------------------------------------------------------


async def test_rtc_objects_built_inside_running_loop(prompts, rtc, events):
    async with answering(ROOM, reason="no_did") as ans:
        await ans.play("end")
    assert rtc.built_in_loop == [True, True]


async def test_play_pushes_whole_prompt_then_waits_for_playout(prompts, rtc, events):
    async with answering(ROOM, reason="unmapped_did", workflow_run_id=None) as ans:
        await ans.play("transfer")
    src = rtc.sources[0]
    assert src.frames == len(prompts) // la.FRAME_BYTES
    assert src.playout_waited
    ok = [e for e in events if e["event"] == "answer.ok"]
    assert len(ok) == 1 and ok[0]["stage"] == "transfer" and ok[0]["elapsed_ms"] >= 0
    assert rtc.rooms[0].disconnected and src.closed


async def test_never_subscribes(prompts, rtc, events, monkeypatch):
    from api.services.pipecat import livekit_dispatcher

    grants = {}
    real = livekit_dispatcher._sign_agent_token

    def spy(room, identity, **kw):
        grants.update(kw, identity=identity)
        return real(room, identity, **kw)

    monkeypatch.setattr(livekit_dispatcher, "_sign_agent_token", spy)
    async with answering(ROOM, reason="no_did"):
        pass
    assert rtc.options.auto_subscribe is False
    assert grants["can_subscribe"] is False and grants["can_publish_data"] is False
    assert grants["can_publish_sources"] == ["microphone"]
    assert grants["identity"].startswith("agent-answer-")  # reconciler skips agent-*
    assert len(grants["identity"]) == len("agent-answer-") + 8  # no room / number


async def test_non_cs_room_refused_before_joining(prompts, rtc, events):
    with pytest.raises(AnswerFailed) as ei:
        async with answering("queue-_+886287654321_x", reason="no_did"):
            pass
    assert ei.value.stage == "room"
    assert rtc.rooms == []
    assert events[0]["event"] == "answer.failed" and events[0]["stage"] == "room"


async def test_connect_timeout(prompts, rtc, events, monkeypatch):
    monkeypatch.setattr(la, "CONNECT_TIMEOUT_SECONDS", 0.05)
    rtc.connect_hang = True
    with pytest.raises(AnswerFailed) as ei:
        async with answering(ROOM, reason="no_did"):
            pass
    assert ei.value.stage == "connect"
    assert events[-1]["event"] == "answer.failed" and events[-1]["stage"] == "connect"
    assert rtc.rooms[0].disconnected  # no leaked connection
    assert la._active == 0


async def test_publish_failure(prompts, rtc, events):
    rtc.publish_error = RuntimeError("engine: not connected")
    with pytest.raises(AnswerFailed) as ei:
        async with answering(ROOM, reason="no_did"):
            pass
    assert ei.value.stage == "publish"
    assert rtc.rooms[0].disconnected and rtc.sources[0].closed


async def test_play_exception(prompts, rtc, events):
    rtc.capture_error = RuntimeError("InvalidState - failed to capture frame")
    with pytest.raises(AnswerFailed) as ei:
        async with answering(ROOM, reason="no_did") as ans:
            await ans.play("end")
    assert ei.value.stage == "play"
    assert [e["stage"] for e in events if e["event"] == "answer.failed"] == ["play"]


async def test_capture_that_never_returns_hits_the_bound(
    prompts, rtc, events, monkeypatch
):
    """AC9: a stuck capture_frame cannot hold the participant past its bound."""
    monkeypatch.setattr(la, "PLAY_SLACK_SECONDS", 0.05)
    rtc.capture_hang = True
    t0 = time.monotonic()
    with pytest.raises(AnswerFailed) as ei:
        async with answering(ROOM, reason="no_did") as ans:
            await ans.play("end")
    assert ei.value.stage == "play"
    assert time.monotonic() - t0 < 2.0


async def test_overall_budget_binds_as_deadline(prompts, rtc, events, monkeypatch):
    """Replaying past the summed budget (one play per prompt) ends as ``deadline``."""
    monkeypatch.setattr(la, "PLAY_SLACK_SECONDS", 0.2)
    rtc.capture_hang = True
    async with answering(ROOM, reason="no_did") as ans:
        ans._budget = 0.05  # less than one play's own limit
        with pytest.raises(AnswerFailed) as ei:
            await ans.play("end")
    assert ei.value.stage == "deadline"


async def test_disconnect_that_never_returns_is_abandoned(
    prompts, rtc, events, monkeypatch
):
    monkeypatch.setattr(la, "DISCONNECT_TIMEOUT_SECONDS", 0.05)
    rtc.disconnect_hang = True
    t0 = time.monotonic()
    async with answering(ROOM, reason="no_did") as ans:
        await ans.play("end")
    assert time.monotonic() - t0 < 1.0
    assert la._active == 0


async def test_cancel_disconnects_and_releases_slot(prompts, rtc, events):
    rtc.capture_hang = True
    started = asyncio.Event()

    async def run():
        async with answering(ROOM, reason="no_did") as ans:
            started.set()
            await ans.play("end")

    task = asyncio.create_task(run())
    await asyncio.wait_for(started.wait(), 1.0)
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert rtc.rooms[0].disconnected and rtc.sources[0].closed
    assert la._active == 0


async def test_caller_left_during_play(prompts, rtc, events):
    """AC13: the SIP caller leaving stops the prompt with stage caller_left."""
    rtc.capture_hang = True

    async def hang_up_soon():
        await asyncio.sleep(0.02)
        rtc.rooms[0].handlers["participant_disconnected"](types.SimpleNamespace(kind=3))

    with pytest.raises(AnswerFailed) as ei:
        async with answering(ROOM, reason="undispatched") as ans:
            asyncio.ensure_future(hang_up_soon())
            await ans.play("transfer")
    assert ei.value.stage == "caller_left"
    failed = [e for e in events if e["event"] == "answer.failed"]
    assert failed[0]["stage"] == "caller_left"


async def test_non_sip_leaving_is_not_caller_left(prompts, rtc, events, monkeypatch):
    monkeypatch.setattr(la, "PLAY_SLACK_SECONDS", 0.1)
    rtc.capture_hang = True

    async def agent_leaves():
        await asyncio.sleep(0.02)
        rtc.rooms[0].handlers["participant_disconnected"](types.SimpleNamespace(kind=0))

    with pytest.raises(AnswerFailed) as ei:
        async with answering(ROOM, reason="no_did") as ans:
            asyncio.ensure_future(agent_leaves())
            await ans.play("end")
    assert ei.value.stage == "play"


async def test_concurrency_limit(prompts, rtc, events, monkeypatch):
    """AC12: past the shared limit nothing joins and answer.saturated is emitted."""
    monkeypatch.setattr(la, "_active", la.MAX_CONCURRENT_ANSWERS)
    with pytest.raises(AnswerFailed) as ei:
        async with answering(ROOM, reason="capacity_overflow"):
            pass
    assert ei.value.stage == "saturated"
    assert rtc.rooms == []
    assert [e["event"] for e in events] == ["answer.saturated"]
    assert la._active == la.MAX_CONCURRENT_ANSWERS  # untouched


async def test_slots_are_shared_and_released(prompts, rtc, events):
    gate = asyncio.Event()

    async def hold():
        async with answering(ROOM, reason="no_did"):
            await gate.wait()

    tasks = [asyncio.create_task(hold()) for _ in range(la.MAX_CONCURRENT_ANSWERS)]
    await asyncio.sleep(0.01)
    assert la._active == la.MAX_CONCURRENT_ANSWERS
    with pytest.raises(AnswerFailed):
        async with answering(ROOM, reason="no_did"):
            pass
    gate.set()
    await asyncio.gather(*tasks)
    assert la._active == 0


async def test_events_carry_no_number_or_header(prompts, rtc, events):
    rtc.publish_error = RuntimeError("+886912345678 X-Ticket-Auth: v1.k1")
    with pytest.raises(AnswerFailed):
        async with answering(ROOM, reason="no_did", workflow_run_id=5):
            pass
    (ev,) = [e for e in events if e["event"] == "answer.failed"]
    assert set(ev) <= {
        "event",
        "room_name",
        "reason",
        "workflow_run_id",
        "elapsed_ms",
        "stage",
    }
    assert "+886912345678" not in repr(ev) and "Ticket" not in repr(ev)


# --- 2.7 alert routing -------------------------------------------------------------------


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setenv("OBS_ALERT_WEBHOOK_URL", "http://alerts.example/hook")
    monkeypatch.setattr(alerts, "spawn", lambda coro: (out.append(coro), coro.close()))
    monkeypatch.setattr(alerts, "_format", _record_format(out))
    return out


def _record_format(out):
    real = alerts._format

    def fmt(event, fields):
        text = real(event, fields)
        out.append(text)
        return text

    return fmt


def _texts(sent):
    return [x for x in sent if isinstance(x, str)]


def test_answer_failed_is_immediate_with_stage(sent):
    alerts.notify(
        "answer.failed", {"room_name": ROOM, "reason": "no_did", "stage": "publish"}
    )
    assert _texts(sent) == [
        f"[answer.failed] room_name={ROOM} reason=no_did stage=publish"
    ]


def test_caller_left_is_log_only(sent):
    alerts.notify(
        "answer.failed", {"room_name": ROOM, "reason": "no_did", "stage": "caller_left"}
    )
    assert sent == []


def test_answer_disabled_is_immediate(sent):
    alerts.notify(
        "answer.disabled", {"room_name": "", "reason": "answer_end.wav: missing"}
    )
    assert len(_texts(sent)) == 1


def test_answer_saturated_is_windowed():
    assert "answer.saturated" in alerts.WINDOWED_EVENTS
    assert "answer.saturated" not in alerts.IMMEDIATE_EVENTS
    assert "answer.ok" not in alerts.IMMEDIATE_EVENTS | alerts.WINDOWED_EVENTS
