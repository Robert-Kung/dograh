"""A stalled Gemini TTS stream is never silent on a LIVEKIT call (C4; 5.1 review S1).

The Gemini API client applies no timeout of its own: a TTS request that the
upstream (or a proxy) accepts and then never answers used to hold the
utterance open with no bound. Mid-reply, nothing else was waiting either —
the bot speaking had disarmed bot_silence, and the idle clock only starts on
``BotStoppedSpeakingFrame``, which a stalled utterance never sends.

pipecat's ``GeminiTTSService`` now gives up once a chunk (the first included)
takes longer than ``stream_idle_timeout_secs`` and pushes a non-fatal
``ErrorFrame``. This file wires that to the call's exit with the real TTS
service built by ``service_factory`` and the real safetynet watchdog: the
error is a TTS error, TTS is marked unavailable, and the call is transferred
without a prompt (design D9, TTS row).

The factory sets the timeout to 3 s (``GEMINI_TTS_STREAM_IDLE_TIMEOUT_SECS``),
the same as the playback start timeout;
the tests shorten it on the built service so they run quickly. Responses are
test doubles; nothing reaches the network.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame, TTSSpeakFrame
from pipecat.services.google.tts import GeminiTTSStreamTimeoutError
from pipecat.tests.utils import run_test

from api.services.pipecat.livekit_failure_exits import PLAYBACK_START_TIMEOUT_SECONDS
from api.services.pipecat.service_factory import (
    GEMINI_LLM_FIRST_CHUNK_TIMEOUT_SECS,
    GEMINI_TTS_STREAM_IDLE_TIMEOUT_SECS,
    create_tts_service,
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

# run_pipeline's default max_user_idle_timeout, plus the 0.35 s pipecat's output
# transport waits before BotStoppedSpeakingFrame starts the idle clock.
DEFAULT_USER_IDLE_TIMEOUT = 10.0
BOT_STOPPED_DELAY = 0.35

# design D9: TTS-unavailable continuous silence, provisional cap (1.4).
SILENCE_BUDGET = 20.0

# A "stalled" stream goes quiet far beyond the timeout but not forever, so that
# without the timeout the test fails on its assertions instead of hanging.
STALL = 3.0

AUDIO_CONFIG = SimpleNamespace(
    transport_in_sample_rate=16000, transport_out_sample_rate=16000
)

# One second of 24 kHz 16-bit audio: pushed downstream as soon as it arrives.
ONE_SECOND = b"\x00\x01" * 24000


def _gemini_tts():
    user_config = SimpleNamespace(
        tts=SimpleNamespace(
            provider="google_gemini",
            api_key=FAKE_KEY,
            model="gemini-3.8-flash-lite-tts",
            voice="Kore",
            language="cmn-TW",
        )
    )
    return create_tts_service(user_config, AUDIO_CONFIG)


def _audio_chunk():
    part = SimpleNamespace(inline_data=SimpleNamespace(data=ONE_SECOND), text=None)
    return SimpleNamespace(
        candidates=[
            SimpleNamespace(content=SimpleNamespace(parts=[part]), finish_reason=None)
        ],
        prompt_feedback=None,
    )


def _stalling_client(chunks_before_stall):
    """A client whose stream the API accepted and that then stops producing."""

    async def generate_content_stream(*args, **kwargs):
        async def stream():
            for _ in range(chunks_before_stall):
                yield _audio_chunk()
            await asyncio.sleep(STALL)

        return stream()

    client = MagicMock()
    client.aio.models.generate_content_stream = generate_content_stream
    return client


def test_factory_bounds_the_gemini_tts_stream():
    """3 s per chunk, the first included: the same first-audio allowance as
    the playback start timeout."""
    tts = _gemini_tts()

    assert tts._stream_idle_timeout_secs == GEMINI_TTS_STREAM_IDLE_TIMEOUT_SECS == 3.0
    assert GEMINI_TTS_STREAM_IDLE_TIMEOUT_SECS <= PLAYBACK_START_TIMEOUT_SECONDS


def test_idle_turn_with_a_stalled_tts_fits_the_silence_budget():
    """The idle-timeout turn arms no bot_silence. Its worst TTS case: the LLM
    is healthy but answers just under its first-chunk bound, then the TTS
    stalls before its first chunk; the TTS timeout marks TTS unavailable and
    the REFER goes out with no prompt. 10 + 0.35 + 6 + 3 ≈ 19.4 s ≤ 20 s
    (design D9; with a 5 s TTS timeout this was ≈ 21.4 s)."""
    assert (
        DEFAULT_USER_IDLE_TIMEOUT
        + BOT_STOPPED_DELAY
        + GEMINI_LLM_FIRST_CHUNK_TIMEOUT_SECS
        + GEMINI_TTS_STREAM_IDLE_TIMEOUT_SECS
        <= SILENCE_BUDGET
    )


@pytest.mark.parametrize(
    "chunks_before_stall",
    [0, 1],
    ids=["stalls-before-speaking", "stalls-mid-reply"],
)
async def test_stalled_gemini_tts_transfers_to_a_human(
    services, refer, chunks_before_stall
):
    call = await ready_call(make_call(services, refer))
    tts = _gemini_tts()
    tts._stream_idle_timeout_secs = TEST_IDLE_TIMEOUT

    with patch.object(tts, "_client", _stalling_client(chunks_before_stall)):
        down, up = await asyncio.wait_for(
            run_test(
                tts,
                frames_to_send=[TTSSpeakFrame(text="好的，我幫您查詢。")],
                observers=[call.wd],
            ),
            timeout=10,
        )

    # The audio that did arrive is played; the rest of the utterance is given up.
    audio = [f for f in down if isinstance(f, TTSAudioRawFrame)]
    assert bool(audio) == (chunks_before_stall > 0)

    # The error is non-fatal and comes from the TTS, so the pipeline stays up.
    [error] = [f for f in up if isinstance(f, ErrorFrame)]
    assert isinstance(error.exception, GeminiTTSStreamTimeoutError)
    assert error.fatal is False
    assert error.processor is tts

    # The safetynet takes it as a TTS failure: no prompt, REFER, normal end.
    await until(lambda: call.engine.entries("end"))
    assert call.state.tts_unavailable
    assert call.engine.log == [("refer", True), ("end", TRANSFERRED, False)]
    await call.wd.stop()


def test_tts_timeout_is_classified_as_a_tts_error():
    """Classification is by the processor that pushed the error, so a timeout
    from the factory-built service lands in the TTS row of design D9."""
    from api.services.pipecat.livekit_failure_exits import (
        ErrorSource,
        classify_error_source,
    )

    tts = _gemini_tts()
    exc = GeminiTTSStreamTimeoutError(3.0, text_length=9, chunks_received=0)
    frame = ErrorFrame(error=str(exc), exception=exc, processor=tts)

    assert classify_error_source(frame) is ErrorSource.TTS
