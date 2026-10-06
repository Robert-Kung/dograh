"""Caller-number normalisation, masking and keyed hashing (ccp W4c 設計 C).

One normaliser for both the write at call connect and the search query, so
every spelling of a number lands on the same HMAC. It is deliberately not
``normalize_telephony_address``: that helper strips ``+`` before matching the
country code (a foreign number gets 886 prepended) and is called without a
country hint on the caller path (``0912…`` becomes ``+0912…``).

Only ASCII digits and ``+ - ( )`` / space are accepted — a full-width digit is
rejected rather than folded, so the console can tell the user the input is
malformed instead of silently searching for something else.
"""

import base64
import binascii
import hashlib
import hmac
import os
import re

_ALLOWED = re.compile(r"[0-9+\-() ]+")
_SEPARATORS = re.compile(r"[\-() ]")
_E164 = re.compile(r"\+\d{8,15}")
_HEX = re.compile(r"(?:[0-9a-fA-F]{2})+")
_FINGERPRINT_LABEL = b"ccp-key-id"
MIN_KEY_BYTES = 32


def normalize_caller(raw: object) -> str | None:
    """E.164 for a Taiwan-hinted caller number, or None when not a number.

    ``0…`` and ``886…`` get the Taiwan country code; ``+…`` is kept as is
    (``+8860…`` loses the trunk 0). SIP URIs, extensions, ``00`` international
    prefixes and anything with other characters are not numbers here.
    """
    if not isinstance(raw, str) or not _ALLOWED.fullmatch(raw):
        return None
    s = _SEPARATORS.sub("", raw)
    if "+" in s[1:]:
        return None
    if s.startswith("+"):
        e164 = s
    elif s.startswith("886"):
        e164 = "+" + s
    elif s.startswith("0") and not s.startswith("00"):
        e164 = "+886" + s[1:]
    else:
        return None
    if e164.startswith("+8860"):
        e164 = "+886" + e164[5:]
    return e164 if _E164.fullmatch(e164) else None


def last4(e164: str) -> str:
    return e164[-4:]


def mask(e164: str) -> str:
    return "***" + last4(e164)


def decode_key(value: str | None) -> bytes | None:
    """Hex or base64 key of at least 32 bytes; anything else is "no key"."""
    value = (value or "").strip()
    if not value:
        return None
    if _HEX.fullmatch(value):
        key = bytes.fromhex(value)
    else:
        try:
            key = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            return None
    return key if len(key) >= MIN_KEY_BYTES else None


def hmac_key() -> bytes | None:
    return decode_key(os.environ.get("CALLER_NUMBER_HMAC_KEY"))


def caller_hmac(key: bytes, e164: str) -> str:
    return hmac.new(key, e164.encode(), hashlib.sha256).hexdigest()


def key_fingerprint(key: bytes) -> str:
    return hmac.new(key, _FINGERPRINT_LABEL, hashlib.sha256).hexdigest()[:8]
