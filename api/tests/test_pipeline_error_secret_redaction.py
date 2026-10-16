"""API keys never leave the process through pipeline error events (gemini-key-voice 1.1).

Design D8: an ErrorFrame's ``error`` and its original exception (string and
attributes) become external data at two outputs -- the realtime feedback
pipeline error event (WebSocket + logs buffer, stored as the run's
``realtime_feedback_events``) and the safetynet ``provider.error`` call event.
Both redact API-key-shaped strings and ``key=`` URL query values. Every test
runs with both Google key formats: the ``AIza`` API key and the ``AQ.`` auth key
that AI Studio issues by default since 2026.
"""

import json
import types

import pytest
from google.genai import errors as genai_errors
from pipecat.frames.frames import ErrorFrame
from pipecat.observers.base_observer import FramePushed
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from api.services.observability import call_events
from api.services.pipecat.in_memory_buffers import InMemoryLogsBuffer
from api.services.pipecat.livekit_safetynet import SafetynetWatchdog
from api.services.pipecat.realtime_feedback_observer import RealtimeFeedbackObserver
from api.utils.secret_redaction import REDACTED, redact_secrets

# Fixed fake keys (design D8 scan rule: the 1.1/1.8 unit-test values, one per
# key format).
FAKE_API_KEY = "AIza" + "0" * 35
FAKE_AUTH_KEY = "AQ." + "0" * 50
# A query-string secret that is not Google-key shaped: only the ``key=`` rule
# can catch it.
QUERY_SECRET = "query-secret-value-123"
KEY_URI = (
    "https://generativelanguage.googleapis.com/v1beta/models/m:streamGenerateContent"
    f"?alt=sse&key={QUERY_SECRET}&x=1"
)


@pytest.fixture(params=[FAKE_API_KEY, FAKE_AUTH_KEY], ids=["api_key", "auth_key"])
def fake_key(request) -> str:
    return request.param


def _leaks(text: str, fake_key: str) -> bool:
    return fake_key in text or QUERY_SECRET in text or "AIza" in text or "AQ." in text


def _api_error(fake_key: str) -> genai_errors.ClientError:
    """A google-genai error whose string and attributes all carry both secrets."""
    return genai_errors.ClientError(
        400,
        {
            "error": {
                "code": 400,
                "status": "INVALID_ARGUMENT",
                "message": f"API key {fake_key} not valid; request {KEY_URI}",
                "details": [
                    {
                        "reason": "API_KEY_INVALID",
                        "metadata": {"key": fake_key, "url": KEY_URI},
                        "links": [KEY_URI, fake_key],
                    }
                ],
            }
        },
        None,
    )


async def _error_frame_from_push_error(
    fake_key: str, *, fatal: bool = False
) -> ErrorFrame:
    """Build the ErrorFrame the way services do: ``push_error(error_msg=, exception=)``."""
    processor = FrameProcessor(name="GeminiSTTService#0")
    pushed = []

    async def capture(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append((frame, direction))

    processor.push_frame = capture
    try:
        raise _api_error(fake_key)
    except genai_errors.ClientError as exc:
        await processor.push_error(
            error_msg=f"connection error: {exc} (url {KEY_URI}, key {fake_key})",
            exception=exc,
            fatal=fatal,
        )
    [(frame, direction)] = pushed
    assert direction == FrameDirection.UPSTREAM
    assert isinstance(frame, ErrorFrame)
    # Precondition: the secrets really are in every input the observer reads.
    assert fake_key in frame.error and QUERY_SECRET in frame.error
    assert fake_key in str(frame.exception) and QUERY_SECRET in str(frame.exception)
    assert fake_key in frame.exception.message
    assert QUERY_SECRET in json.dumps(frame.exception.details)
    return frame


@pytest.mark.asyncio
async def test_pipeline_error_event_redacts_secrets_in_every_field(fake_key):
    frame = await _error_frame_from_push_error(fake_key)

    sent = []

    async def ws_sender(message):
        sent.append(message)

    logs_buffer = InMemoryLogsBuffer(workflow_run_id=1)
    observer = RealtimeFeedbackObserver(ws_sender=ws_sender, logs_buffer=logs_buffer)
    await observer.on_push_frame(
        FramePushed(
            source=frame.processor,
            destination=types.SimpleNamespace(),
            frame=frame,
            direction=FrameDirection.UPSTREAM,
            timestamp=0,
        )
    )

    stored = logs_buffer.get_events()
    assert len(sent) == 1 and len(stored) == 1
    for event in (sent[0], stored[0]):
        assert not _leaks(json.dumps(event), fake_key)
        payload = event["payload"]
        assert REDACTED in payload["error"]
        assert REDACTED in payload["exception_message"]
        assert REDACTED in payload["message"]
        assert REDACTED in json.dumps(payload["details"])
        # Kept verbatim: what the application uses to classify the error.
        assert payload["exception_type"] == "ClientError"
        assert payload["code"] == 400
        assert payload["status"] == "INVALID_ARGUMENT"
        assert payload["fatal"] is False


@pytest.mark.asyncio
async def test_safetynet_provider_error_reason_is_redacted(monkeypatch, fake_key):
    frame = await _error_frame_from_push_error(fake_key)
    captured = []
    monkeypatch.setattr(
        call_events, "emit", lambda event, **fields: captured.append((event, fields))
    )

    async def on_fatal(reason):
        pass

    wd = SafetynetWatchdog(
        on_fatal=on_fatal, threshold_seconds=8.0, poll_seconds=999, room_name="cs-x"
    )
    await wd.on_push_frame(types.SimpleNamespace(frame=frame))
    await wd.stop()

    [(event, fields)] = captured
    assert event == "provider.error"
    assert not _leaks(fields["reason"], fake_key)
    assert REDACTED in fields["reason"]


@pytest.mark.asyncio
async def test_safetynet_redacts_before_truncating(monkeypatch, fake_key):
    # A key straddling the 200-character cut: truncating first would leave a
    # key prefix that no longer matches the redaction pattern.
    captured = []
    monkeypatch.setattr(
        call_events, "emit", lambda event, **fields: captured.append(fields["reason"])
    )

    async def on_fatal(reason):
        pass

    wd = SafetynetWatchdog(on_fatal=on_fatal, threshold_seconds=8.0, poll_seconds=999)
    await wd.on_push_frame(
        types.SimpleNamespace(frame=ErrorFrame("x" * 190 + fake_key))
    )
    await wd.on_push_frame(
        types.SimpleNamespace(frame=ErrorFrame("x" * 180 + f"?key={QUERY_SECRET}"))
    )
    await wd.stop()

    assert len(captured) == 2
    for reason in captured:
        assert len(reason) <= 200
        assert fake_key[:3] not in reason
        assert "query-se" not in reason


def test_redact_secrets_handles_nested_values_and_leaves_others_alone(fake_key):
    value = {
        "msg": f"bad {fake_key}",
        fake_key: [KEY_URI, ("t", fake_key)],
        "n": 1008,
        "none": None,
        "flag": True,
    }
    out = redact_secrets(value)
    assert not _leaks(json.dumps(out, default=list), fake_key)
    assert out["n"] == 1008 and out["none"] is None and out["flag"] is True
    assert isinstance(out[REDACTED][1], tuple)
    # Only the ``key`` query parameter is redacted; neighbours are kept.
    assert out[REDACTED][0].endswith(f"?alt=sse&key={REDACTED}&x=1")
    # ``api_key=`` style text that is not a URL query parameter is left alone.
    assert redact_secrets("monkey=1 api_key=abc") == "monkey=1 api_key=abc"


def test_auth_key_pattern_redacts_any_run_of_20_or_more():
    # The issued auth keys are ``AQ.`` + 50 characters; shorter or longer runs
    # are redacted too (Google does not document the length). Plain text that
    # merely contains ``AQ.`` is left alone.
    for n in (20, 50, 80):
        assert redact_secrets(f"k={'AQ.' + 'a' * n};") == f"k={REDACTED};"
    assert redact_secrets("AQ.short and FAQ. notes") == "AQ.short and FAQ. notes"
