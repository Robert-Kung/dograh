"""Redact API-key-shaped secrets from text that leaves the process.

Used at the outputs where pipeline errors become external data: the realtime
feedback pipeline error event (WebSocket, logs buffer, then the run's stored
``realtime_feedback_events``) and the safetynet ``provider.error`` call event.
An exception string can carry a request URL, and some SDKs put the API key in
the URL query, so every string is redacted before it is sent or stored.
"""

import re
from typing import Any

REDACTED = "[REDACTED]"

# Google API keys (Gemini API keys included).
_GOOGLE_API_KEY = re.compile(r"AIza[0-9A-Za-z_-]{35}")
# The value of a ``key`` URL query parameter, whatever its format.
_URL_QUERY_KEY = re.compile(r"([?&]key=)[^&#\s'\"]+")


def redact_secrets(value: Any) -> Any:
    """Return ``value`` with API-key-shaped strings replaced by ``REDACTED``.

    Strings are redacted; dicts (keys and values), lists and tuples are
    processed recursively; anything else is returned unchanged. Callers that
    truncate must redact first, so a key cut in half cannot slip through.
    """
    if isinstance(value, str):
        value = _GOOGLE_API_KEY.sub(REDACTED, value)
        return _URL_QUERY_KEY.sub(rf"\g<1>{REDACTED}", value)
    if isinstance(value, dict):
        return {redact_secrets(k): redact_secrets(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact_secrets(v) for v in value)
    return value
