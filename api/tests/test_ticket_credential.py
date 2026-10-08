"""Reflow ticket credential — signer half (ccp R-AV.3 tasks 2.6/2.7/4.3/4.4).

The vectors file is a byte-identical copy of the platform queue's
`services/queue/tests/ticket_auth_v1_vectors.json` (the verifier side);
signing them here and verifying them there pins one wire format.
"""

import base64
import json
import re
from pathlib import Path

import pytest
from loguru import logger

import api.services.pipecat.transfer_context_handoff as handoff
from api.services.tickets import credential

VECTORS = json.loads(
    (Path(__file__).parent / "support" / "ticket_auth_v1_vectors.json").read_text()
)
KEY = base64.urlsafe_b64decode(VECTORS["key_b64url"] + "=")
CRED_RE = re.compile(
    r"v1\.([A-Za-z0-9_-]{1,16})\.([0-9]{1,12})\.([A-Za-z0-9_-]{22})"
    r"\.([A-Za-z0-9_-]{1,64})\.([A-Za-z0-9_-]{43})"
)
ENV = {
    credential.KEYS_ENV: json.dumps({"k0": "AAAA", "k1": VECTORS["key_b64url"]}),
    credential.KID_ENV: "k1",
}
SHORT_KEY = "AAECAwQFBgcICQoLDA0ODw"  # 128-bit


@pytest.fixture
def logs():
    lines: list[str] = []
    sink = logger.add(lines.append, format="{message}")
    yield lines
    logger.remove(sink)


@pytest.mark.parametrize("vec", VECTORS["valid"], ids=lambda v: v["kid"])
def test_signer_reproduces_cross_component_vectors(vec):
    assert (
        credential.sign(KEY, vec["kid"], vec["exp"], vec["nonce"], vec["ticket_id"])
        == vec["credential"]
    )


def test_tampered_vector_is_not_what_the_signer_produces():
    tampered = next(v for v in VECTORS["rejected"] if v["reason"] == "bad_mac")
    _, kid, exp, nonce, ticket_id, _ = tampered["credential"].split(".")
    assert (
        credential.sign(KEY, kid, int(exp), nonce, ticket_id) != tampered["credential"]
    )


@pytest.mark.parametrize("bad", ["", "CS-1;encoding=ascii", "CS 1", "x" * 65])
def test_ticket_outside_shape_is_refused_not_split(bad):
    # review security L-2: no `;` canonicalisation on either side.
    with pytest.raises(ValueError):
        credential.sign(KEY, "k1", 1, "A" * 22, bad)


def test_issue_fresh_nonce_and_signer_side_expiry():
    config = credential.SignerConfig(kid="k1", key=KEY, ttl_s=180)
    a = credential.issue(config, "CS-7", now=1_900_000_000.9)
    b = credential.issue(config, "CS-7", now=1_900_000_000.9)
    ma, mb = CRED_RE.fullmatch(a), CRED_RE.fullmatch(b)
    assert ma and mb
    assert ma.group(1) == "k1" and ma.group(4) == "CS-7"
    assert int(ma.group(2)) == 1_900_000_180
    assert ma.group(3) != mb.group(3)
    assert a == credential.sign(KEY, "k1", 1_900_000_180, ma.group(3), "CS-7")


def test_config_unset_is_none_and_ttl_defaults():
    assert credential.load_signer_config({}) is None
    config = credential.load_signer_config(ENV)
    assert config == credential.SignerConfig(kid="k1", key=KEY, ttl_s=180)
    assert credential.DEFAULT_TTL_S == 180
    assert (
        credential.load_signer_config({**ENV, credential.TTL_ENV: "240"}).ttl_s == 240
    )


@pytest.mark.parametrize(
    "env",
    [
        {credential.KEYS_ENV: ENV[credential.KEYS_ENV]},  # kid missing
        {credential.KID_ENV: "k1"},  # keys missing
        {**ENV, credential.KID_ENV: "has.dot"},
        {**ENV, credential.KID_ENV: "k2"},  # kid not in the map
        {**ENV, credential.KEYS_ENV: "not json"},
        {**ENV, credential.KEYS_ENV: json.dumps(["k1"])},
        {**ENV, credential.KEYS_ENV: json.dumps({"k1": "not base64!"})},
        {**ENV, credential.KEYS_ENV: json.dumps({"k1": SHORT_KEY})},
        {**ENV, credential.TTL_ENV: "0"},
        {**ENV, credential.TTL_ENV: "-5"},
        {**ENV, credential.TTL_ENV: "3m"},
    ],
)
def test_unusable_config_raises_without_echoing_the_key(env):
    with pytest.raises(credential.SignerConfigError) as exc:
        credential.load_signer_config(env)
    assert VECTORS["key_b64url"] not in str(exc.value)


def test_issue_ticket_credential_configured(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    cred = handoff.issue_ticket_credential("CS-9")
    assert CRED_RE.fullmatch(cred).group(4) == "CS-9"


def test_unconfigured_issues_nothing_and_counts(monkeypatch, logs):
    monkeypatch.delenv(credential.KEYS_ENV, raising=False)
    monkeypatch.delenv(credential.KID_ENV, raising=False)
    before = handoff.TICKET_AUTH_METRICS["unsigned"]
    assert handoff.issue_ticket_credential("CS-9") == ""
    assert handoff.TICKET_AUTH_METRICS["unsigned"] == before + 1
    # distinct from the ticket-write failure marker (review L-14)
    assert not any("context_write" in line for line in logs)


def test_invalid_config_issues_nothing_and_never_logs_key(monkeypatch, logs):
    monkeypatch.setenv(credential.KEYS_ENV, json.dumps({"k1": SHORT_KEY}))
    monkeypatch.setenv(credential.KID_ENV, "k1")
    assert handoff.issue_ticket_credential("CS-9") == ""
    assert any("ticket_auth: unsigned (reason=invalid_config)" in x for x in logs)
    assert not any(SHORT_KEY in line for line in logs)


def test_signing_exception_issues_nothing(monkeypatch, logs):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    # a ticket id outside the shape makes sign() raise
    assert handoff.issue_ticket_credential("CS 9") == ""
    assert any("ticket_auth: unsigned (reason=ValueError)" in x for x in logs)
    assert not any(VECTORS["key_b64url"] in line for line in logs)


def test_refer_headers_with_and_without_credential():
    plan = handoff.HandoffPlan(
        config=None,
        ticket_id="CS-1",
        workflow_run_id=1,
        organization_id=1,
        caller_number="",
        room_name="r",
        transfer_reason="x",
    )
    assert plan.refer_headers == {handoff.UUI_HEADER: "CS-1;encoding=ascii"}
    plan.ticket_auth = "v1.k1.1.AAAAAAAAAAAAAAAAAAAAAA.CS-1." + "A" * 43
    assert plan.refer_headers == {
        handoff.UUI_HEADER: "CS-1;encoding=ascii",
        handoff.TICKET_AUTH_HEADER: plan.ticket_auth,
    }
    # the credential never rides the ARQ snapshot
    assert plan.ticket_auth not in json.dumps(plan.to_job_snapshot("success"))
