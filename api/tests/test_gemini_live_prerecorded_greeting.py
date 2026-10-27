"""gemini-key-voice 2.1: a recorded greeting seeds Gemini Live without a reply."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import TTSSpeakFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection

from api.services.pipecat.realtime.gemini_live import DograhGeminiLiveLLMService

GREETING = "您好，本通電話將會錄音。"


class _TestService(DograhGeminiLiveLLMService):
    def create_client(self):
        self._client = SimpleNamespace(
            aio=SimpleNamespace(live=SimpleNamespace(connect=None))
        )


class _FakeSession:
    def __init__(self):
        self.send_client_content = AsyncMock()
        self.send_realtime_input = AsyncMock()
        self.send_tool_response = AsyncMock()
        self.close = AsyncMock()


def _make_service(model: str = "gemini-3.8-live") -> _TestService:
    service = _TestService(api_key="test-key", model=model)
    service.stop_all_metrics = AsyncMock()
    service.start_ttfb_metrics = AsyncMock()
    service.cancel_task = AsyncMock()
    service.push_error = AsyncMock()
    return service


def _context() -> LLMContext:
    return LLMContext(messages=[{"role": "system", "content": "客服"}])


def _sent_turns(session):
    kwargs = session.send_client_content.await_args.kwargs
    return kwargs["turns"], kwargs["turn_complete"]


@pytest.mark.asyncio
async def test_seeds_the_heard_greeting_as_a_model_turn_without_a_reply():
    service = _make_service()
    session = _FakeSession()
    service._session = session

    await service.handle_prerecorded_greeting(_context(), GREETING)

    turns, turn_complete = _sent_turns(session)
    assert turn_complete is False
    assert turns[-1].role == "model"
    assert turns[-1].parts[0].text == GREETING
    # Gemini 3 only generates after a realtime-input nudge; none may be sent.
    session.send_realtime_input.assert_not_awaited()
    assert service._ready_for_realtime_input is True


@pytest.mark.asyncio
async def test_seed_waits_for_the_session_and_does_not_greet_again():
    service = _make_service()
    await service.handle_prerecorded_greeting(_context(), GREETING)
    assert service._run_llm_when_session_ready is True

    session = _FakeSession()
    await service._handle_session_ready(session)

    session.send_client_content.assert_awaited_once()
    turns, turn_complete = _sent_turns(session)
    assert turn_complete is False
    assert turns[-1].parts[0].text == GREETING
    session.send_realtime_input.assert_not_awaited()
    assert service._pending_prerecorded_greeting is None


@pytest.mark.asyncio
async def test_later_greeting_triggers_do_not_open_a_second_time():
    service = _make_service()
    session = _FakeSession()
    service._session = session
    await service.handle_prerecorded_greeting(_context(), GREETING)

    await service.handle_prerecorded_greeting(_context(), GREETING)
    await service.process_frame(TTSSpeakFrame("再說一次"), FrameDirection.DOWNSTREAM)

    session.send_client_content.assert_awaited_once()
    session.send_realtime_input.assert_not_awaited()


@pytest.mark.asyncio
async def test_gemini_2_5_seed_ends_on_a_user_turn():
    service = _make_service("gemini-2.5-flash-native-audio")
    session = _FakeSession()
    service._session = session

    await service.handle_prerecorded_greeting(_context(), GREETING)

    turns, turn_complete = _sent_turns(session)
    assert turn_complete is False
    assert [t.role for t in turns[-2:]] == ["model", "user"]
    assert service._needs_initial_turn_complete_message is True


@pytest.mark.asyncio
async def test_untranscribed_recording_still_opens_the_input_gate():
    service = _make_service()
    session = _FakeSession()
    service._session = session

    await service.handle_prerecorded_greeting(_context(), None)

    assert all(getattr(t, "role", None) != "model" for t in _sent_turns(session)[0])
    assert service._ready_for_realtime_input is True
    assert service._handled_initial_context is True
