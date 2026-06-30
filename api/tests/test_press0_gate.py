"""Press-0 gate tests (S-L3-PRESS0 §4.4).

debounce_ok is pure and runs anywhere. The frame-routing tests instantiate the
real Press0Gate (needs the pipecat FrameProcessor base) and are skipped where
pipecat is not installed; they run in CI/Docker.
"""

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

def _make_gate(monkeypatch, *, now_values):
    """Build a Press0Gate with a fake engine/clock and a recording execute()."""
    from api.services.pipecat import press0_gate as mod
    from api.services.pipecat.press0_gate import Press0Gate

    calls = {"execute": [], "pushed": [], "interrupts": 0}

    async def fake_execute(engine, **kwargs):
        calls["execute"].append(kwargs)
        return {"status": "success", "action": "transferred"}

    monkeypatch.setattr(mod, "execute_cold_transfer", fake_execute)

    clock = iter(now_values)
    gate = Press0Gate(
        engine=object(),
        room_name="room1",
        config={"destination": "tel:+886912345678", "schedule": None},
        debounce_seconds=0.5,
        monotonic=lambda: next(clock),
    )

    async def fake_push(frame, direction):
        calls["pushed"].append(frame)

    async def fake_interrupt():
        calls["interrupts"] += 1

    gate.push_frame = fake_push
    gate.broadcast_interruption = fake_interrupt
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

    assert len(calls["execute"]) == 1
    assert calls["execute"][0]["destination"] == "tel:+886912345678"
    assert calls["interrupts"] == 1  # barge-in before transfer
    assert calls["pushed"] == []  # 0 swallowed, not forwarded


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

    assert len(calls["execute"]) == 1  # only one transfer
    assert calls["pushed"] == []  # both 0s swallowed


@pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")
async def test_repeat_after_window_triggers_again(monkeypatch):
    from pipecat.processors.frame_processor import FrameDirection

    gate, calls = _make_gate(monkeypatch, now_values=[1000.0, 1000.7])
    await gate.process_frame(_dtmf("0"), FrameDirection.DOWNSTREAM)
    await gate.process_frame(_dtmf("0"), FrameDirection.DOWNSTREAM)

    assert len(calls["execute"]) == 2  # outside window → triggers again
