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

The factory sets the timeout to 10 s (``GEMINI_STREAM_IDLE_TIMEOUT_SECS``);
the tests shorten it on the built service so they run in well under a second.

When the stalled turn is empty, the fallback prompt ("please say that again")
and the safetynet's transfer prompt can both be spoken, in either order: the
fallback goes downstream at the turn's end while the transfer runs in a
background task off the ErrorFrame. Accepted (review R4): the caller hears one
extra sentence before the transfer and is never left in silence.

The fakes (engine, SIP REFER) are shared with ``test_livekit_failure_exits``.
"""

import asyncio
from unittest.mock import patch

import pytest
from google.genai.types import Candidate, Content, GenerateContentResponse, Part
from pipecat.frames.frames import (
    ErrorFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.tests.utils import run_test
from tests.test_livekit_failure_exits import (  # noqa: F401 (fixtures)
    FAKE_KEY,
    TRANSFERRED,
    _call,
    _no_leaked_background_tasks,
    _ready,
    _reset_latch,
    _until,
    refer,
    services,
)

from api.services.configuration.registry import ServiceProviders
from api.services.pipecat import livekit_safetynet as sn
from api.services.pipecat.empty_llm_turn_fallback import (
    EMPTY_LLM_TURN_FALLBACK_TEXT,
    EmptyLLMTurnFallbackProcessor,
)
from api.services.pipecat.service_factory import (
    GEMINI_STREAM_IDLE_TIMEOUT_SECS,
    create_llm_service_from_provider,
)

# Short enough to keep the tests quick, long enough to outlast scheduling jitter.
TEST_IDLE_TIMEOUT = 0.2


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
    """10 s, not pipecat's 20 s; and longer than bot_silence, which comes first
    while a reply is owed — the timeout covers a stream that stalls after the
    bot has started speaking, when no other clock is running."""
    llm = _gemini_llm()

    assert llm._stream_idle_timeout_secs == GEMINI_STREAM_IDLE_TIMEOUT_SECS == 10.0
    assert llm._retry_on_timeout is False
    assert GEMINI_STREAM_IDLE_TIMEOUT_SECS > sn._DEFAULT_MAX_SILENCE_SECONDS


@pytest.mark.parametrize(
    "first_text",
    [None, "好的，我幫您查"],
    ids=["stalls-before-speaking", "stalls-after-speaking"],
)
async def test_stalled_gemini_stream_transfers_to_a_human(
    services,  # noqa: F811 (fixture shared with test_livekit_failure_exits)
    refer,  # noqa: F811
    first_text,
):
    call = await _ready(_call(services, refer))
    llm = _gemini_llm()
    llm._stream_idle_timeout_secs = TEST_IDLE_TIMEOUT

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
    await _until(lambda: call.engine.entries("end"))
    assert [e[0] for e in call.engine.log] == ["tts", "refer", "end"]
    assert "轉接專員" in call.engine.log[0][1]
    assert call.engine.log[2] == ("end", TRANSFERRED, False)
    assert call.state.llm_gate_closed
    await call.wd.stop()
