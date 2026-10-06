"""ccp W4c caller-number normalisation, masking and HMAC."""

import base64

import pytest

from api.services.ccp import caller_identity as ci

KEY = bytes(range(32))


@pytest.mark.parametrize(
    "raw",
    [
        "+886912345678",
        "0912345678",
        "0912-345-678",
        "+886 912 345 678",
        "+8860912345678",
        "886912345678",
        "(09) 1234-5678",
    ],
)
def test_taiwan_spellings_normalise_to_one_e164(raw):
    assert ci.normalize_caller(raw) == "+886912345678"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+14155551234", "+14155551234"),
        ("+1 415 555 1234", "+14155551234"),
        ("+81312345678", "+81312345678"),
        ("0212345678", "+886212345678"),
    ],
)
def test_foreign_numbers_keep_their_country_code(raw, expected):
    assert ci.normalize_caller(raw) == expected
    if not raw.startswith("0"):
        assert "886" not in ci.normalize_caller(raw)


@pytest.mark.parametrize(
    "raw,expected",
    [("0911000001", "+886911000001"), ("+886911000002", "+886911000002")],
)
def test_sip_phone_number_as_measured(raw, expected):
    # task 0.5: livekit-sip passes the From user part as is (identity
    # ``sip_<raw>``); the harness sent both spellings and both matched
    assert ci.normalize_caller(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "anonymous",
        "sip:+886912345678@carrier.example",
        "0912345678;ext=12",
        "1001",  # extension
        "０９１２３４５６７８",  # full-width digits are rejected, not folded
        "0912 345678",  # no-break space
        "+886+912345678",
        "00114155551234",  # international dial prefix: ambiguous
        "+1234567",  # too short
        "+1234567890123456",  # too long
        12345678,
    ],
)
def test_non_numbers_are_none(raw):
    assert ci.normalize_caller(raw) is None


def test_mask_and_last4():
    assert ci.mask("+886912345678") == "***5678"
    assert ci.last4("+886912345678") == "5678"


def test_hmac_is_keyed_and_stable():
    a = ci.caller_hmac(KEY, "+886912345678")
    assert a == ci.caller_hmac(KEY, "+886912345678")
    assert a != ci.caller_hmac(bytes(32), "+886912345678")
    assert len(a) == 64 and "912345678" not in a


@pytest.mark.parametrize(
    "value,ok",
    [
        (KEY.hex(), True),
        (base64.b64encode(KEY).decode(), True),
        (bytes(31).hex(), False),  # 31 bytes
        (base64.b64encode(bytes(31)).decode(), False),
        ("not-a-key!", False),
        ("", False),
        (None, False),
    ],
)
def test_decode_key(value, ok):
    assert (ci.decode_key(value) is not None) is ok


def test_missing_key_is_none_not_raise(monkeypatch):
    monkeypatch.delenv("CALLER_NUMBER_HMAC_KEY", raising=False)
    assert ci.hmac_key() is None
    monkeypatch.setenv("CALLER_NUMBER_HMAC_KEY", "short")
    assert ci.hmac_key() is None
    monkeypatch.setenv("CALLER_NUMBER_HMAC_KEY", KEY.hex())
    assert ci.hmac_key() == KEY


def test_fingerprint_differs_per_key():
    assert ci.key_fingerprint(KEY) != ci.key_fingerprint(bytes(32))
    assert len(ci.key_fingerprint(KEY)) == 8
