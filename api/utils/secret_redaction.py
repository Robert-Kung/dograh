"""Redact API keys from text that leaves the process.

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
# Google auth keys, the format AI Studio issues by default since 2026: ``AQ.``
# followed by 50 characters in the keys seen so far. Google documents neither
# the length nor the character set, so the rule errs on the wide side: any run
# of 20 or more URL-safe, base64 or percent-encoding characters (``.``, ``~``,
# ``+``, ``/``, ``=`` and ``%`` included) is treated as a key. Over-redacting a
# neighbouring character is acceptable; leaving part of a key is not.
_GOOGLE_AUTH_KEY = re.compile(r"AQ\.[0-9A-Za-z._~+/=%-]{20,}")
# The value of a ``key`` URL query parameter, whatever its format.
_URL_QUERY_KEY = re.compile(r"([?&]key=)[^&#\s'\"]+")
# Bearer tokens and JSON ``accessToken`` fields, whatever the provider's key format.
_BEARER = re.compile(r"(Bearer\s+)[^\s'\",}]+")
_ACCESS_TOKEN = re.compile(r"(['\"]accessToken['\"]\s*:\s*['\"])[^'\"]*")

# Configured provider keys, redacted by value whatever their shape. Keys shorter
# than this are not registered: replacing short strings would mangle ordinary text.
_MIN_REGISTERED_LENGTH = 12
_registered: set[str] = set()


def register_secret(value: Any) -> None:
    """Remember a configured credential so ``redact_secrets`` removes it by value.

    Accepts a string or a list of strings (multi-key configurations); anything
    else is ignored.
    """
    values = value if isinstance(value, (list, tuple)) else [value]
    for item in values:
        if isinstance(item, str) and len(item.strip()) >= _MIN_REGISTERED_LENGTH:
            _registered.add(item.strip())


def redact_secrets(value: Any) -> Any:
    """Return ``value`` with API-key-shaped strings replaced by ``REDACTED``.

    Strings are redacted; dicts (keys and values), lists and tuples are
    processed recursively; anything else is returned unchanged. Callers that
    truncate must redact first, so a key cut in half cannot slip through.
    """
    if isinstance(value, str):
        for secret in sorted(_registered, key=len, reverse=True):
            value = value.replace(secret, REDACTED)
        value = _BEARER.sub(rf"\g<1>{REDACTED}", value)
        value = _ACCESS_TOKEN.sub(rf"\g<1>{REDACTED}", value)
        value = _GOOGLE_API_KEY.sub(REDACTED, value)
        value = _GOOGLE_AUTH_KEY.sub(REDACTED, value)
        return _URL_QUERY_KEY.sub(rf"\g<1>{REDACTED}", value)
    if isinstance(value, dict):
        return {redact_secrets(k): redact_secrets(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact_secrets(v) for v in value)
    return value
