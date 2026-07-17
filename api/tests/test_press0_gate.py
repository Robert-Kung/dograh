"""Press-0 gate tests (S-L3-PRESS0 §4.4).

debounce_ok is pure and runs anywhere. The frame-routing tests instantiate the
real Press0Gate (needs the pipecat FrameProcessor base) and are skipped where
pipecat is not installed; they run in CI/Docker.
"""

import types

import pytest

try:
    # press0_gate imports the pipecat runtime + loguru at module load.
    from api.services.pipecat.press0_gate import debounce_ok

    PIPECAT = True
except ImportError:
    PIPECAT = False

pytestmark = pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")


# --- debounce (pure) ------------------------------------------------------


def test_first_press_always_triggers():
    assert debounce_ok(float("-inf"), 100.0, 0.5) is True


def test_repeat_within_window_blocked():
    assert debounce_ok(100.0, 100.3, 0.5) is False


def test_repeat_after_window_allowed():
    assert debounce_ok(100.0, 100.6, 0.5) is True


def test_window_boundary_inclusive():
    assert debounce_ok(100.0, 100.5, 0.5) is True


# --- gate frame routing (needs pipecat) -----------------------------------


def _make_gate(monkeypatch, *, now_values, execute_result=None):
    """Build a Press0Gate with a fake engine/clock and a recording execute().

    The transfer runs via self.create_task; the fake create_task collects the
    coroutine so tests can await it deterministically via ``drain``.
    """
    from api.services.pipecat import press0_gate as mod
    from api.services.pipecat.press0_gate import Press0Gate

    result = execute_result or {"status": "success", "action": "transferred"}
    calls = {"execute": [], "pushed": [], "interrupts": 0, "queued": [], "tasks": []}

    async def fake_execute(engine, **kwargs):
        calls["execute"].append(kwargs)
        return result

    monkeypatch.setattr(mod, "execute_cold_transfer", fake_execute)

    async def queue_frame(frame):
        calls["queued"].append(frame)

    engine = types.SimpleNamespace(task=types.SimpleNamespace(queue_frame=queue_frame))

    clock = iter(now_values)
    gate = Press0Gate(
        engine=engine,
        room_name="room1",
        config={"destination": "tel:+886912345678", "schedule": None},
        debounce_seconds=0.5,
        monotonic=lambda: next(clock),
    )

    async def fake_push(frame, direction):
        calls["pushed"].append(frame)

    async def fake_interrupt():
        calls["interrupts"] += 1

    def fake_create_task(coro, name=None):
        calls["tasks"].append(coro)
        return None

    gate.push_frame = fake_push
    gate.broadcast_interruption = fake_interrupt
    gate.create_task = fake_create_task

    async def drain():
        for coro in calls["tasks"]:
            await coro
        calls["tasks"].clear()

    calls["drain"] = drain
    return gate, calls


def _dtmf(digit):
    from pipecat.audio.dtmf.types import KeypadEntry
    from pipecat.frames.frames import InputDTMFFrame

    return InputDTMFFrame(button=KeypadEntry(digit))


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_press_zero_transfers_and_is_swallowed(monkeypatch):
    from pipecat.processors.frame_processor import FrameDirection

    gate, calls = _make_gate(monkeypatch, now_values=[1000.0])
    await gate.process_frame(_dtmf("0"), FrameDirection.DOWNSTREAM)
    assert calls["interrupts"] == 1  # barge-in fires inline, before the task
    assert calls["pushed"] == []  # 0 swallowed, not forwarded
    await calls["drain"]()  # transfer runs off the frame loop

    assert len(calls["execute"]) == 1
    assert calls["execute"][0]["destination"] == "tel:+886912345678"
    assert calls["queued"] == []  # success → no fallback announcement


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_non_zero_key_passes_through(monkeypatch):
    from pipecat.processors.frame_processor import FrameDirection

    gate, calls = _make_gate(monkeypatch, now_values=[1000.0])
    frame = _dtmf("5")
    await gate.process_frame(frame, FrameDirection.DOWNSTREAM)

    assert calls["execute"] == []  # no transfer
    assert calls["pushed"] == [frame]  # forwarded untouched


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_non_dtmf_frame_passes_through(monkeypatch):
    from pipecat.frames.frames import Frame
    from pipecat.processors.frame_processor import FrameDirection

    gate, calls = _make_gate(monkeypatch, now_values=[1000.0])
    frame = Frame()
    await gate.process_frame(frame, FrameDirection.DOWNSTREAM)

    assert calls["execute"] == []
    assert calls["pushed"] == [frame]


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_rapid_repeat_triggers_once(monkeypatch):
    from pipecat.processors.frame_processor import FrameDirection

    # Two presses 0.2s apart — second is inside the 0.5s debounce window.
    gate, calls = _make_gate(monkeypatch, now_values=[1000.0, 1000.2])
    await gate.process_frame(_dtmf("0"), FrameDirection.DOWNSTREAM)
    await gate.process_frame(_dtmf("0"), FrameDirection.DOWNSTREAM)
    await calls["drain"]()

    assert len(calls["execute"]) == 1  # only one transfer
    assert calls["pushed"] == []  # both 0s swallowed


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_repeat_after_window_triggers_again(monkeypatch):
    from pipecat.processors.frame_processor import FrameDirection

    gate, calls = _make_gate(monkeypatch, now_values=[1000.0, 1000.7])
    await gate.process_frame(_dtmf("0"), FrameDirection.DOWNSTREAM)
    await gate.process_frame(_dtmf("0"), FrameDirection.DOWNSTREAM)
    await calls["drain"]()

    assert len(calls["execute"]) == 2  # outside window → triggers again


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_refer_failure_announces_fallback(monkeypatch):
    from pipecat.frames.frames import TTSSpeakFrame
    from pipecat.processors.frame_processor import FrameDirection

    from api.services.pipecat.press0_gate import _DEFAULT_FAILURE_MESSAGE

    gate, calls = _make_gate(
        monkeypatch,
        now_values=[1000.0],
        execute_result={
            "status": "failed",
            "action": "transfer_failed",
            "reason": "sip_refer_error",
        },
    )
    await gate.process_frame(_dtmf("0"), FrameDirection.DOWNSTREAM)
    await calls["drain"]()

    # Caller is never left silent on failure (C4): a fallback is spoken.
    assert len(calls["queued"]) == 1
    frame = calls["queued"][0]
    assert isinstance(frame, TTSSpeakFrame)
    assert frame.text == _DEFAULT_FAILURE_MESSAGE


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_already_transferring_is_silent(monkeypatch):
    from pipecat.processors.frame_processor import FrameDirection

    # The concurrent trigger owns the transfer; the loser must not double-announce.
    gate, calls = _make_gate(
        monkeypatch,
        now_values=[1000.0],
        execute_result={
            "status": "failed",
            "action": "transfer_failed",
            "reason": "already_transferring",
        },
    )
    await gate.process_frame(_dtmf("0"), FrameDirection.DOWNSTREAM)
    await calls["drain"]()

    assert calls["queued"] == []  # no announcement


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_transfer_crash_still_announces_fallback(monkeypatch):
    """C4 belt: if the executor ever raises (e.g. malformed config leaking
    through), the off-loop task must announce the failure, not die silently
    inside the TaskManager (PR #8 review H1)."""
    from pipecat.processors.frame_processor import FrameDirection

    from api.services.pipecat import press0_gate as mod

    gate, calls = _make_gate(monkeypatch, now_values=[1000.0])

    async def exploding_execute(engine, **kwargs):
        raise ValueError("malformed config leaked")

    monkeypatch.setattr(mod, "execute_cold_transfer", exploding_execute)
    await gate.process_frame(_dtmf("0"), FrameDirection.DOWNSTREAM)
    await calls["drain"]()

    texts = [getattr(f, "text", "") for f in calls["queued"]]
    assert len(texts) == 1 and texts[0]  # spoken fallback, no dead silence
