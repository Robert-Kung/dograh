"""LLM 一輪結束卻沒有可說的文字、也沒有呼叫工具時，補一句固定話術（C4 永不死寂）。

實測（2026-10-15）：LM Studio 上的 gemma 拿到工具結果後會間歇只吐推理標記就 stop，
pipecat 不推任何 LLMTextFrame；TTS 也會丟掉純空白文字。結果電話上是一片沉默、
文字對話是空回覆。本處理器接在 LLM 之後（即時與文字兩條管線同一位置），在
LLMFullResponseEndFrame 之後推一個 TTSSpeakFrame，並寫入 context，讓下一輪的 LLM
看得到自己剛請對方再說一次。

**連續空輪只補第一次。** LLM 出錯（400、429、連不上）時各家 service 也是
push_error 後照推 End，看起來就是空輪；若每輪都補話，bot 會一直請對方再說一次，
而 LiveKit safetynet 的 bot_silence 計時被補話解除、永遠轉不到真人。第二個連續空輪
保持沉默，交回 safetynet 轉真人（livekit_safetynet.py）。有內容的一輪才重新計數。

**對方插話的輪不補。** 只看 InterruptionFrame 擋不住首字前插話：LLM 的生成 task
被中斷取消時，finally 照推 LLMFullResponseEndFrame，而這個 End 比 LLM 隨後往下
轉送的 InterruptionFrame 先到這裡——兜底會在中斷抵達前送出、由 TTS 寫進脈絡，
逐字稿多一行沒播出的 AI 發言。使用者聚合器在廣播 InterruptionFrame 之前先廣播
UserStartedSpeakingFrame（pipecat LLMUserAggregator._on_user_turn_started），
它是 SystemFrame，經 LLM 的 input task 依序轉送，必定早於那個 End 抵達。所以：
回應進行中收到 UserStartedSpeakingFrame 即視為被插話；對方還在說話
（UserStarted 之後、UserStopped 之前）時結束的空輪也不補——涵蓋 LLM 在對方開口後
才推出 LLMFullResponseStartFrame 的競態。被插話的輪不改變連續空輪的計數。
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
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

EMPTY_LLM_TURN_FALLBACK_TEXT = (
    "不好意思，我這邊剛剛沒有處理好，可以請您再說一次您的問題嗎？"
)


def _is_function_call(frame: Frame) -> bool:
    # 工具參數全部解析失敗時，OpenAI 相容 service 仍推 function_calls=[] 的 Info frame，
    # 實際上什麼都不會執行——那一輪仍算空輪。
    if isinstance(frame, (FunctionCallsFromLLMInfoFrame, FunctionCallsStartedFrame)):
        return bool(frame.function_calls)
    return isinstance(frame, FunctionCallInProgressFrame)


class EmptyLLMTurnFallbackProcessor(FrameProcessor):
    def __init__(self, fallback_text: str = EMPTY_LLM_TURN_FALLBACK_TEXT):
        super().__init__()
        self._fallback_text = fallback_text
        self._in_response = False
        self._spoke = False
        self._fell_back_last_turn = False
        self._user_speaking = False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMFullResponseStartFrame):
            self._in_response = True
            self._spoke = False
        elif isinstance(frame, (InterruptionFrame, UserStartedSpeakingFrame)):
            # 被插話打斷的那一輪不補話：對方正在說。
            self._in_response = False
            if isinstance(frame, UserStartedSpeakingFrame):
                self._user_speaking = True
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self._user_speaking = False
        elif self._in_response and (
            (isinstance(frame, LLMTextFrame) and frame.text.strip())
            or _is_function_call(frame)
        ):
            self._spoke = True

        await self.push_frame(frame, direction)

        if isinstance(frame, LLMFullResponseEndFrame) and self._in_response:
            self._in_response = False
            if self._user_speaking:
                return
            if self._spoke:
                self._fell_back_last_turn = False
            elif not self._fell_back_last_turn:
                self._fell_back_last_turn = True
                await self.push_frame(
                    TTSSpeakFrame(
                        self._fallback_text,
                        append_to_context=True,
                        persist_to_logs=True,
                    )
                )
