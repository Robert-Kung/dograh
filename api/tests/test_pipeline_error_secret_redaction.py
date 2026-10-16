"""API keys never leave the process through pipeline error events (gemini-key-voice 1.1).

Design D8: an ErrorFrame's ``error`` and its original exception (string and
attributes) become external data at two outputs -- the realtime feedback
pipeline error event (WebSocket + logs buffer, stored as the run's
``realtime_feedback_events``) and the safetynet ``provider.error`` call event.
Both redact API-key-shaped strings and ``key=`` URL query values.
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

# Fixed fake key (design D8 scan rule: the 1.1/1.8 unit-test value).
FAKE_KEY = "AIza" + "0" * 35
# A query-string secret that is not Google-key shaped: only the ``key=`` rule
# can catch it.
QUERY_SECRET = "query-secret-value-123"
KEY_URI = (
    "https://generativelanguage.googleapis.com/v1beta/models/m:streamGenerateContent"
    f"?alt=sse&key={QUERY_SECRET}&x=1"
)


def _leaks(text: str) -> bool:
    return FAKE_KEY in text or QUERY_SECRET in text or "AIza" in text


def _api_error() -> genai_errors.ClientError:
    """A google-genai error whose string and attributes all carry both secrets."""
    return genai_errors.ClientError(
        400,
        {
            "error": {
                "code": 400,
                "status": "INVALID_ARGUMENT",
                "message": f"API key {FAKE_KEY} not valid; request {KEY_URI}",
                "details": [
                    {
                        "reason": "API_KEY_INVALID",
                        "metadata": {"key": FAKE_KEY, "url": KEY_URI},
                        "links": [KEY_URI, FAKE_KEY],
                    }
                ],
            }
        },
        None,
    )


async def _error_frame_from_push_error(*, fatal: bool = False) -> ErrorFrame:
    """Build the ErrorFrame the way services do: ``push_error(error_msg=, exception=)``."""
    processor = FrameProcessor(name="GeminiSTTService#0")
    pushed = []

    async def capture(frame, direction=FrameDirection.DOWNSTREAM):
        pushed.append((frame, direction))

    processor.push_frame = capture
    try:
        raise _api_error()
    except genai_errors.ClientError as exc:
        await processor.push_error(
            error_msg=f"connection error: {exc} (url {KEY_URI}, key {FAKE_KEY})",
            exception=exc,
            fatal=fatal,
        )
    [(frame, direction)] = pushed
    assert direction == FrameDirection.UPSTREAM
    assert isinstance(frame, ErrorFrame)
    # Precondition: the secrets really are in every input the observer reads.
    assert FAKE_KEY in frame.error and QUERY_SECRET in frame.error
    assert FAKE_KEY in str(frame.exception) and QUERY_SECRET in str(frame.exception)
    assert FAKE_KEY in frame.exception.message
    assert QUERY_SECRET in json.dumps(frame.exception.details)
    return frame


@pytest.mark.asyncio
async def test_pipeline_error_event_redacts_secrets_in_every_field():
    frame = await _error_frame_from_push_error()

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
        assert not _leaks(json.dumps(event))
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
async def test_safetynet_provider_error_reason_is_redacted(monkeypatch):
    frame = await _error_frame_from_push_error()
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
    assert not _leaks(fields["reason"])
    assert REDACTED in fields["reason"]


@pytest.mark.asyncio
async def test_safetynet_redacts_before_truncating(monkeypatch):
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
        types.SimpleNamespace(frame=ErrorFrame("x" * 190 + FAKE_KEY))
    )
    await wd.on_push_frame(
        types.SimpleNamespace(frame=ErrorFrame("x" * 180 + f"?key={QUERY_SECRET}"))
    )
    await wd.stop()

    assert len(captured) == 2
    for reason in captured:
        assert len(reason) <= 200
        assert "AIza" not in reason
        assert "query-se" not in reason


def test_redact_secrets_handles_nested_values_and_leaves_others_alone():
    value = {
        "msg": f"bad {FAKE_KEY}",
        FAKE_KEY: [KEY_URI, ("t", FAKE_KEY)],
        "n": 1008,
        "none": None,
        "flag": True,
    }
    out = redact_secrets(value)
    assert not _leaks(json.dumps(out, default=list))
    assert out["n"] == 1008 and out["none"] is None and out["flag"] is True
    assert isinstance(out[REDACTED][1], tuple)
    # Only the ``key`` query parameter is redacted; neighbours are kept.
    assert out[REDACTED][0].endswith(f"?alt=sse&key={REDACTED}&x=1")
    # ``api_key=`` style text that is not a URL query parameter is left alone.
    assert redact_secrets("monkey=1 api_key=abc") == "monkey=1 api_key=abc"
