"""空 LLM 輪兜底（C4）：沒文字也沒工具呼叫的一輪結束時補一句固定話術。"""

import pytest

try:
    from pipecat.frames.frames import (
        FunctionCallFromLLM,
        FunctionCallsFromLLMInfoFrame,
        InterruptionFrame,
        LLMContextFrame,
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        LLMTextFrame,
        TTSSpeakFrame,
    )
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.frame_processor import FrameDirection

    from api.services.pipecat.empty_llm_turn_fallback import (
        EMPTY_LLM_TURN_FALLBACK_TEXT,
        EmptyLLMTurnFallbackProcessor,
    )

    PIPECAT = True
except ImportError:
    PIPECAT = False

pytestmark = pytest.mark.skipif(not PIPECAT, reason="pipecat runtime not installed")


def _processor():
    proc = EmptyLLMTurnFallbackProcessor()
    pushed = []

    async def fake_push(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append(frame)

    proc.push_frame = fake_push
    return proc, pushed


async def _feed(proc, *frames):
    for frame in frames:
        await proc.process_frame(frame, FrameDirection.DOWNSTREAM)


def _fallbacks(pushed):
    return [f for f in pushed if isinstance(f, TTSSpeakFrame)]


async def test_empty_turn_speaks_fallback_after_end():
    proc, pushed = _processor()
    await _feed(proc, LLMFullResponseStartFrame(), LLMFullResponseEndFrame())

    assert isinstance(pushed[-2], LLMFullResponseEndFrame)
    (fallback,) = _fallbacks(pushed)
    assert pushed[-1] is fallback
    assert fallback.text == EMPTY_LLM_TURN_FALLBACK_TEXT
    assert fallback.append_to_context is True


async def test_whitespace_only_text_counts_as_empty():
    proc, pushed = _processor()
    await _feed(
        proc,
        LLMFullResponseStartFrame(),
        LLMTextFrame(text="  "),
        LLMTextFrame(text="\n"),
        LLMFullResponseEndFrame(),
    )
    assert len(_fallbacks(pushed)) == 1


async def test_turn_with_text_is_left_alone():
    proc, pushed = _processor()
    await _feed(
        proc,
        LLMFullResponseStartFrame(),
        LLMTextFrame(text="廢機車可以領三百元。"),
        LLMFullResponseEndFrame(),
    )
    assert _fallbacks(pushed) == []


async def test_tool_call_turn_is_left_alone():
    proc, pushed = _processor()
    call = FunctionCallFromLLM(
        function_name="search_knowledge",
        tool_call_id="c1",
        arguments={"query": "廢機車"},
        context=LLMContext(),
    )
    await _feed(
        proc,
        LLMFullResponseStartFrame(),
        FunctionCallsFromLLMInfoFrame(function_calls=[call]),
        LLMFullResponseEndFrame(),
    )
    assert _fallbacks(pushed) == []


async def test_interrupted_turn_is_left_alone():
    proc, pushed = _processor()
    await _feed(
        proc,
        LLMFullResponseStartFrame(),
        InterruptionFrame(),
        LLMFullResponseEndFrame(),
    )
    assert _fallbacks(pushed) == []


async def test_end_without_start_is_ignored_and_state_resets_per_turn():
    proc, pushed = _processor()
    await _feed(proc, LLMFullResponseEndFrame())
    assert _fallbacks(pushed) == []

    await _feed(
        proc,
        LLMFullResponseStartFrame(),
        LLMTextFrame(text="第一輪有回答。"),
        LLMFullResponseEndFrame(),
        LLMFullResponseStartFrame(),
        LLMFullResponseEndFrame(),
    )
    assert len(_fallbacks(pushed)) == 1


async def test_all_frames_pass_through_in_order():
    proc, pushed = _processor()
    frames = [
        LLMContextFrame(context=LLMContext()),
        LLMFullResponseStartFrame(),
        LLMFullResponseEndFrame(),
    ]
    await _feed(proc, *frames)
    assert pushed[:3] == frames
