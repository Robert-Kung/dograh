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
        UserStartedSpeakingFrame,
        UserStoppedSpeakingFrame,
    )
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.frame_processor import FrameDirection
    from pipecat.tests.mock_llm_service import MockLLMService
    from pipecat.tests.utils import SleepFrame, run_test

    from api.services.pipecat.empty_llm_turn_fallback import (
        EMPTY_LLM_TURN_FALLBACK_TEXT,
        CallerBargeInFrame,
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


async def test_tool_call_frame_with_no_parsed_calls_counts_as_empty():
    proc, pushed = _processor()
    await _feed(
        proc,
        LLMFullResponseStartFrame(),
        FunctionCallsFromLLMInfoFrame(function_calls=[]),
        LLMFullResponseEndFrame(),
    )
    assert len(_fallbacks(pushed)) == 1


async def test_consecutive_empty_turns_fall_back_only_once():
    """持續出錯（每輪都空）時第二輪起保持沉默，交回 safetynet 轉真人。"""
    proc, pushed = _processor()
    for _ in range(3):
        await _feed(proc, LLMFullResponseStartFrame(), LLMFullResponseEndFrame())
    assert len(_fallbacks(pushed)) == 1

    # 有內容的一輪之後重新計數
    await _feed(
        proc,
        LLMFullResponseStartFrame(),
        LLMTextFrame(text="好的。"),
        LLMFullResponseEndFrame(),
        LLMFullResponseStartFrame(),
        LLMFullResponseEndFrame(),
    )
    assert len(_fallbacks(pushed)) == 2


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


async def test_user_started_speaking_before_end_suppresses_fallback():
    """插話時 UserStartedSpeakingFrame 先於被取消那輪的 End 到達，那一輪不補。"""
    proc, pushed = _processor()
    await _feed(
        proc,
        LLMFullResponseStartFrame(),
        UserStartedSpeakingFrame(),
        LLMFullResponseEndFrame(),
        InterruptionFrame(),
    )
    assert _fallbacks(pushed) == []


async def test_caller_barge_in_marker_before_end_suppresses_fallback():
    """按 0 轉真人：CallerBargeInFrame 先於被取消那輪的 End 到達，那一輪不補。"""
    proc, pushed = _processor()
    await _feed(
        proc,
        LLMFullResponseStartFrame(),
        CallerBargeInFrame(),
        LLMFullResponseEndFrame(),
        InterruptionFrame(),
    )
    assert _fallbacks(pushed) == []


async def test_missing_user_stopped_does_not_silence_later_empty_turns():
    """UserStopped 遺失（靜音不對稱、VAD 誤觸）不得讓之後的空輪永遠不補。"""
    proc, pushed = _processor()
    await _feed(proc, UserStartedSpeakingFrame(), LLMFullResponseStartFrame(), LLMFullResponseEndFrame())
    assert len(_fallbacks(pushed)) == 1


async def test_turn_with_content_resets_count_even_while_user_speaks():
    """空輪（補）→ 對方說話中出現有內容的一輪 → 再一個空輪：計數已重置，照補。"""
    proc, pushed = _processor()
    await _feed(proc, LLMFullResponseStartFrame(), LLMFullResponseEndFrame())
    await _feed(
        proc,
        UserStartedSpeakingFrame(),
        LLMFullResponseStartFrame(),
        LLMTextFrame("好的"),
        LLMFullResponseEndFrame(),
        UserStoppedSpeakingFrame(),
        LLMFullResponseStartFrame(),
        LLMFullResponseEndFrame(),
    )
    assert len(_fallbacks(pushed)) == 2


async def test_interrupted_turn_does_not_reset_consecutive_empty_count():
    """被插話打斷的輪不算有內容：前一輪已補過話，下一個空輪仍保持沉默交回 safetynet。"""
    proc, pushed = _processor()
    await _feed(proc, LLMFullResponseStartFrame(), LLMFullResponseEndFrame())
    await _feed(
        proc,
        LLMFullResponseStartFrame(),
        UserStartedSpeakingFrame(),
        LLMFullResponseEndFrame(),
        UserStoppedSpeakingFrame(),
        LLMFullResponseStartFrame(),
        LLMFullResponseEndFrame(),
    )
    assert len(_fallbacks(pushed)) == 1


def _thinking_llm():
    # 首字前要「想」1 秒：插話落在 LLM 吐出任何文字之前。
    return MockLLMService(
        mock_chunks=MockLLMService.create_text_chunks("您好，廢機車可以領三百元。"),
        chunk_delay=1.0,
    )


async def test_barge_in_before_first_token_sends_no_fallback_through_real_pipeline():
    """M1：真實 pipecat 中斷流程下，首字前插話不得送出兜底（也就不會寫入脈絡）。

    LLM 的生成 task 被取消時，finally 照推 LLMFullResponseEndFrame，而且這個 End
    比 LLM 往下轉送的 InterruptionFrame 早到本處理器；只看 InterruptionFrame
    擋不住。使用者聚合器在 InterruptionFrame 之前先廣播 UserStartedSpeakingFrame
    （LLMUserAggregator._on_user_turn_started），本測試照同樣順序送。
    """
    down, _ = await run_test(
        Pipeline([_thinking_llm(), EmptyLLMTurnFallbackProcessor()]),
        frames_to_send=[
            LLMContextFrame(context=LLMContext()),
            SleepFrame(sleep=0.2),
            UserStartedSpeakingFrame(),
            InterruptionFrame(),
            SleepFrame(sleep=0.2),
        ],
    )
    assert [type(f) for f in down if isinstance(f, LLMFullResponseEndFrame)], (
        "前提：被取消的 LLM 輪仍推出 End"
    )
    assert _fallbacks(down) == []


async def test_empty_turn_through_real_pipeline_still_falls_back():
    down, _ = await run_test(
        Pipeline([MockLLMService(mock_chunks=[]), EmptyLLMTurnFallbackProcessor()]),
        frames_to_send=[LLMContextFrame(context=LLMContext()), SleepFrame(sleep=0.2)],
    )
    (fallback,) = _fallbacks(down)
    assert fallback.text == EMPTY_LLM_TURN_FALLBACK_TEXT


async def test_press_zero_before_first_token_sends_no_fallback_through_real_pipeline():
    """F1：按 0 沒有 UserStartedSpeakingFrame，靠 CallerBargeInFrame 擋下兜底。"""
    down, _ = await run_test(
        Pipeline([_thinking_llm(), EmptyLLMTurnFallbackProcessor()]),
        frames_to_send=[
            LLMContextFrame(context=LLMContext()),
            SleepFrame(sleep=0.2),
            CallerBargeInFrame(),
            InterruptionFrame(),
            SleepFrame(sleep=0.2),
        ],
    )
    assert [f for f in down if isinstance(f, LLMFullResponseEndFrame)], "前提：被取消的 LLM 輪仍推出 End"
    assert _fallbacks(down) == []
