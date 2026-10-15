"""LLM 一輪結束卻沒有可說的文字、也沒有呼叫工具時，補一句固定話術（C4 永不死寂）。

實測（2026-10-15）：LM Studio 上的 gemma 拿到工具結果後會間歇只吐推理標記就 stop，
pipecat 不推任何 LLMTextFrame；TTS 也會丟掉純空白文字。結果電話上是一片沉默、
文字對話是空回覆。本處理器接在 LLM 之後（即時與文字兩條管線同一位置），在
LLMFullResponseEndFrame 之後推一個 TTSSpeakFrame，並寫入 context，讓下一輪的 LLM
看得到自己剛請對方再說一次。
"""

from pipecat.frames.frames import (
    Frame,
    FunctionCallInProgressFrame,
    FunctionCallsFromLLMInfoFrame,
    FunctionCallsStartedFrame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

EMPTY_LLM_TURN_FALLBACK_TEXT = (
    "不好意思，我這邊剛剛沒有處理好，可以請您再說一次您的問題嗎？"
)

_FUNCTION_CALL_FRAMES = (
    FunctionCallsFromLLMInfoFrame,
    FunctionCallsStartedFrame,
    FunctionCallInProgressFrame,
)


class EmptyLLMTurnFallbackProcessor(FrameProcessor):
    def __init__(self, fallback_text: str = EMPTY_LLM_TURN_FALLBACK_TEXT):
        super().__init__()
        self._fallback_text = fallback_text
        self._in_response = False
        self._spoke = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMFullResponseStartFrame):
            self._in_response = True
            self._spoke = False
        elif isinstance(frame, InterruptionFrame):
            # 被插話打斷的那一輪不補話：對方正在說。
            self._in_response = False
        elif (
            self._in_response and isinstance(frame, LLMTextFrame) and frame.text.strip()
        ):
            self._spoke = True
        elif self._in_response and isinstance(frame, _FUNCTION_CALL_FRAMES):
            self._spoke = True

        await self.push_frame(frame, direction)

        if isinstance(frame, LLMFullResponseEndFrame) and self._in_response:
            self._in_response = False
            if not self._spoke:
                await self.push_frame(
                    TTSSpeakFrame(
                        self._fallback_text,
                        append_to_context=True,
                        persist_to_logs=True,
                    )
                )
