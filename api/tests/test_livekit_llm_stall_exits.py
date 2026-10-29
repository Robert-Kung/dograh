"""A stalled Gemini LLM stream is never silent on a LIVEKIT call (C4).

The Gemini API client applies no timeout of its own: a stream that stops
producing without closing used to hold the turn open until the caller hung up.
pipecat's ``GoogleLLMService`` now gives up once the gap between chunks
exceeds ``stream_idle_timeout_secs``, ends the turn, and pushes a non-fatal
``ErrorFrame`` upstream. This file wires that to the call's exits, with the
real LLM service built by ``service_factory``, the real empty-turn fallback,
and the real safetynet watchdog observing the pipeline:

- the turn ends (``LLMFullResponseEndFrame``) and the error is non-fatal, so
  the pipeline stays up to speak and transfer;
- an empty turn gets the empty-turn fallback prompt; a turn that already spoke
  does not;
- the watchdog classifies the error as an LLM error and exits with
  ``llm_error``: transfer prompt, SIP REFER, the transferred call's end.

The factory sets the gap between chunks to 10 s
(``GEMINI_STREAM_IDLE_TIMEOUT_SECS``) and the wait for the first chunk, which
spans the whole round trip including the model's thinking, to 6 s
(``GEMINI_LLM_FIRST_CHUNK_TIMEOUT_SECS``; 5.1 review S2: the idle-timeout turn
has no bot_silence behind it, so this bound sits inside the 20 s silence
budget of design D9). The tests shorten them on the built service so they run
in well under a second.

When the stalled turn is empty, the fallback prompt ("please say that again")
and the safetynet's transfer prompt can both be spoken, in either order: the
fallback goes downstream at the turn's end while the transfer runs in a
background task off the ErrorFrame. Accepted (review R4): the caller hears one
extra sentence before the transfer and is never left in silence.

The fakes (engine, SIP REFER) are shared with ``test_livekit_failure_exits``
through ``api/tests/support/livekit_failure_fakes.py`` and the fixtures in
``api/tests/conftest.py``.
"""

import asyncio
import time
from unittest.mock import patch

import pytest
from google.genai.types import Candidate, Content, GenerateContentResponse, Part
from pipecat.frames.frames import (
    ErrorFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMMessagesAppendFrame,
    LLMTextFrame,
    TTSSpeakFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.tests.utils import run_test
from pipecat.transports.base_output import BOT_VAD_STOP_SECS

from api.services.configuration.registry import ServiceProviders
from api.services.pipecat import livekit_safetynet as sn
from api.services.pipecat.empty_llm_turn_fallback import (
    EMPTY_LLM_TURN_FALLBACK_TEXT,
    EmptyLLMTurnFallbackProcessor,
)
from api.services.pipecat.livekit_failure_exits import PLAYBACK_START_TIMEOUT_SECONDS
from api.services.pipecat.run_pipeline import DEFAULT_MAX_USER_IDLE_TIMEOUT_SECS
from api.services.pipecat.service_factory import (
    GEMINI_LLM_FIRST_CHUNK_TIMEOUT_SECS,
    GEMINI_STREAM_IDLE_TIMEOUT_SECS,
    create_llm_service_from_provider,
)
from api.tests.support.livekit_failure_fakes import (
    FAKE_KEY,
    TRANSFERRED,
    make_call,
    ready_call,
    until,
)

pytestmark = pytest.mark.usefixtures(
    "reset_safetynet_latch", "no_leaked_background_tasks"
)

# Short enough to keep the tests quick, long enough to outlast scheduling jitter.
TEST_IDLE_TIMEOUT = 0.2

# design D9: TTS-unavailable continuous silence, provisional cap (1.4).
# The other terms of the sums below are read from the code: run_pipeline's
# default max_user_idle_timeout (a workflow's run_configs can raise it, and
# the sums with it), and the delay pipecat's output transport waits before
# BotStoppedSpeakingFrame starts the idle clock.
SILENCE_BUDGET = 20.0

# Scaled-down bot_silence threshold for the timing tests.
TEST_BOT_SILENCE = 0.3


def _gemini_llm():
    return create_llm_service_from_provider(
        provider=ServiceProviders.GOOGLE.value,
        model="gemini-2.5-flash",
        api_key=FAKE_KEY,
    )


def _chunk(text):
    return GenerateContentResponse(
        candidates=[Candidate(content=Content(role="model", parts=[Part(text=text)]))]
    )


def _stalling_stream(first_text):
    """A stream the API accepted that then stops producing without closing."""

    async def stream_content(context):
        async def stream():
            if first_text:
                yield _chunk(first_text)
            await asyncio.Event().wait()

        return stream()

    return stream_content


def test_factory_bounds_the_gemini_stream_beyond_the_owed_reply_threshold():
    """Between chunks: 10 s, not pipecat's 20 s; and longer than bot_silence,
    which comes first while a reply is owed — on a LIVEKIT call this timeout
    matters for a stream that stalls after the bot has started speaking, when
    no other clock runs."""
    llm = _gemini_llm()

    assert llm._stream_idle_timeout_secs == GEMINI_STREAM_IDLE_TIMEOUT_SECS == 10.0
    assert llm._retry_on_timeout is False
    assert GEMINI_STREAM_IDLE_TIMEOUT_SECS > sn._DEFAULT_MAX_SILENCE_SECONDS


def test_factory_bounds_the_first_chunk_apart_from_the_gaps():
    """First chunk: 6 s, thinking included (5.1 review S2).

    The turn the idle timeout starts (caller silent) arms no bot_silence, so
    the first-chunk bound is all that stands between a stalled LLM and the
    exit: idle timeout + BotStopped delay + first chunk + playback start
    timeout must fit the 20 s budget (10 + 0.35 + 6 + 3 ≈ 19.4 s). With 10 s
    for the first chunk too, the path was ≈ 23.4 s. A pro or thinking-heavy
    model in a mode without the safetynet (text chat, other transports) can
    time out before saying anything."""
    llm = _gemini_llm()

    assert llm._first_chunk_timeout_secs == GEMINI_LLM_FIRST_CHUNK_TIMEOUT_SECS == 6.0
    assert (
        DEFAULT_MAX_USER_IDLE_TIMEOUT_SECS
        + BOT_VAD_STOP_SECS
        + GEMINI_LLM_FIRST_CHUNK_TIMEOUT_SECS
        + PLAYBACK_START_TIMEOUT_SECONDS
        <= SILENCE_BUDGET
    )


def test_idle_turn_stalled_after_its_first_chunk_fits_the_budget():
    """5.1 fix review R2: the idle-timeout turn is armed like any owed reply,
    so a stream that stalls after a first chunk TTS cannot speak yet (a half
    sentence, or a whole one still waiting for the turn's end) is bounded by
    bot_silence, not by the 10 s gap timeout: 10 + 0.35 + 8 ≈ 18.4 s."""
    assert (
        DEFAULT_MAX_USER_IDLE_TIMEOUT_SECS
        + BOT_VAD_STOP_SECS
        + sn._DEFAULT_MAX_SILENCE_SECONDS
        <= SILENCE_BUDGET
    )
    assert sn._DEFAULT_MAX_SILENCE_SECONDS < GEMINI_STREAM_IDLE_TIMEOUT_SECS


async def test_idle_turn_stalled_after_half_a_sentence_transfers_on_bot_silence(
    monkeypatch, services, refer
):
    """The idle prompt's reply starts with "好的，" and then the stream stalls.

    A Chinese comma ends no sentence, so TTS has nothing to speak and no
    BotStartedSpeakingFrame comes. The idle turn arms no
    UserStoppedSpeakingFrame either: it is an ``LLMMessagesAppendFrame`` with
    ``run_llm``. Before 5.1 fix review R2 nothing was armed, and the caller
    heard silence until the gap timeout (10 s) — about 26 s in all. Now the
    append arms bot_silence, which transfers well before the gap timeout.
    """
    events = []
    monkeypatch.setattr(
        sn, "log_event", lambda event, **fields: events.append((event, fields))
    )
    call = await ready_call(make_call(services, refer))
    call.wd._clock = time.monotonic
    call.wd._threshold = TEST_BOT_SILENCE
    llm = _gemini_llm()
    # Far past bot_silence: if bot_silence is not armed, this ends the turn
    # and the exit is an llm_error instead.
    llm._stream_idle_timeout_secs = TEST_BOT_SILENCE * 6
    idle_prompt = LLMMessagesAppendFrame(
        [{"role": "user", "content": "The user has been quiet."}], run_llm=True
    )

    with patch.object(llm, "_stream_content", _stalling_stream("好的，")):
        await asyncio.wait_for(
            run_test(
                Pipeline([llm, EmptyLLMTurnFallbackProcessor()]),
                frames_to_send=[idle_prompt, LLMContextFrame(LLMContext())],
                observers=[call.wd],
            ),
            timeout=5,
        )

    await until(lambda: call.engine.entries("end"))
    [triggered] = [f for e, f in events if e == "safetynet.triggered"]
    assert triggered["reason"] == "bot_silence"
    assert [e[0] for e in call.engine.log] == ["tts", "refer", "end"]
    assert call.engine.log[2] == ("end", TRANSFERRED, False)
    await call.wd.stop()


def test_vertex_llm_gets_the_same_bounds():
    """The Vertex path shares the Gemini stream and the same exits."""
    with patch(
        "api.services.pipecat.service_factory.DograhGoogleVertexLLMService"
    ) as mock_service:
        create_llm_service_from_provider(
            provider=ServiceProviders.GOOGLE_VERTEX.value,
            model="gemini-2.5-flash",
            api_key=None,
            credentials='{"type":"service_account"}',
            project_id="demo-project",
            location="us-central1",
        )

    kwargs = mock_service.call_args.kwargs
    assert kwargs["stream_idle_timeout_secs"] == GEMINI_STREAM_IDLE_TIMEOUT_SECS
    assert kwargs["first_chunk_timeout_secs"] == GEMINI_LLM_FIRST_CHUNK_TIMEOUT_SECS


async def test_first_chunk_later_than_the_timeout_ends_the_turn():
    """The first-chunk bound fires on its own: the gap timeout is left at its
    factory value (10 s), so only the first-chunk bound can end this turn."""
    llm = _gemini_llm()
    llm._first_chunk_timeout_secs = TEST_IDLE_TIMEOUT
    down, up = [], []

    async def capture(frame, direction=FrameDirection.DOWNSTREAM):
        (up if direction == FrameDirection.UPSTREAM else down).append(frame)

    async def stream_content(context):
        async def stream():
            # Thinking for longer than the timeout before the first chunk.
            await asyncio.sleep(TEST_IDLE_TIMEOUT * 3)
            yield _chunk("想了很久的回答")

        return stream()

    with (
        patch.object(llm, "push_frame", capture),
        patch.object(llm, "_stream_content", stream_content),
    ):
        await asyncio.wait_for(llm._process_context(LLMContext()), timeout=5)

    assert [f.error for f in up if isinstance(f, ErrorFrame)] == [
        "LLM completion timeout"
    ]
    assert not any(isinstance(f, LLMTextFrame) for f in down)
    assert isinstance(down[-1], LLMFullResponseEndFrame)


@pytest.mark.parametrize(
    "first_text",
    [None, "好的，我幫您查"],
    ids=["stalls-before-speaking", "stalls-after-speaking"],
)
async def test_stalled_gemini_stream_transfers_to_a_human(
    monkeypatch, services, refer, first_text
):
    events = []
    monkeypatch.setattr(
        sn, "log_event", lambda event, **fields: events.append((event, fields))
    )
    call = await ready_call(make_call(services, refer))
    llm = _gemini_llm()
    # Shorten only the bound that applies, leaving the other at its factory
    # value: a stall before any chunk is ended by the first-chunk bound (the
    # idle-timeout turn's only clock, 5.1 review S2), a stall after one by the
    # gap bound.
    if first_text:
        llm._stream_idle_timeout_secs = TEST_IDLE_TIMEOUT
    else:
        llm._first_chunk_timeout_secs = TEST_IDLE_TIMEOUT

    with patch.object(llm, "_stream_content", _stalling_stream(first_text)):
        down, up = await asyncio.wait_for(
            run_test(
                Pipeline([llm, EmptyLLMTurnFallbackProcessor()]),
                frames_to_send=[LLMContextFrame(LLMContext())],
                observers=[call.wd],
            ),
            timeout=5,
        )

    # The turn ends instead of hanging, with whatever text did arrive.
    assert isinstance(down[0], LLMFullResponseStartFrame)
    end = next(i for i, f in enumerate(down) if isinstance(f, LLMFullResponseEndFrame))
    spoken = [f.text for f in down[:end] if isinstance(f, LLMTextFrame)]
    assert spoken == ([first_text] if first_text else [])

    # The error is non-fatal and comes from the LLM, so the pipeline stays up.
    [error] = [f for f in up if isinstance(f, ErrorFrame)]
    assert error.error == "LLM completion timeout"
    assert error.fatal is False
    assert error.processor is llm

    # An empty turn gets the fallback prompt; a turn that spoke doesn't.
    fallbacks = [f.text for f in down if isinstance(f, TTSSpeakFrame)]
    assert fallbacks == ([] if first_text else [EMPTY_LLM_TURN_FALLBACK_TEXT])

    # The safetynet takes the LLM error as an exit: prompt, REFER, normal end.
    await until(lambda: call.engine.entries("end"))
    [triggered] = [f for e, f in events if e == "safetynet.triggered"]
    assert triggered["reason"] == "llm_error"
    assert [e[0] for e in call.engine.log] == ["tts", "refer", "end"]
    assert "轉接專員" in call.engine.log[0][1]
    assert call.engine.log[2] == ("end", TRANSFERRED, False)
    assert call.state.llm_gate_closed
    await call.wd.stop()
