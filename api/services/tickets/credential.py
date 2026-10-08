"""Reflow ticket credential — signer half (ccp R-AV.3 reflow-ticket-header-hmac).

    v1.<kid>.<exp>.<nonce>.<ticket_id>.<mac>

`mac` = base64url-nopad(HMAC-SHA256(k_v1, "v1.<kid>.<exp>.<nonce>.<ticket_id>"))
where k_v1 = HMAC-SHA256(<configured key>, "reflow-ticket-auth:v1"). The
per-version derived key keeps a v1 credential from passing a future v2 check
under the same configured key (design D1 版本降級).

The verifier is the platform queue (`services/queue/app/domain/ticket_auth.py`);
both sides pin byte-identical vectors (`tests/support/ticket_auth_v1_vectors.json`)
— any change to this format is a wire change on both sides. No `;param`
suffix is ever appended: the verifier's strict parse rejects one.

The expiry's single source is this signer (design D2): the verifier has no
TTL setting and only reads `exp`. `REFLOW_AUTH_TTL_S` is therefore the value
the deploy-time check must read.
"""

import base64
import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass
from typing import Mapping

VERSION = "v1"
_VERSION_LABEL = b"reflow-ticket-auth:v1"
MIN_KEY_BYTES = 32  # 256-bit (platform-deployment spec: 密鑰強度)
DEFAULT_TTL_S = 180

KEY_ENV = "REFLOW_AUTH_KEY"
KID_ENV = "REFLOW_AUTH_KID"
TTL_ENV = "REFLOW_AUTH_TTL_S"

_KID_RE = re.compile(r"[A-Za-z0-9_-]{1,16}")
_TICKET_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_KEY_RE = re.compile(r"[A-Za-z0-9_-]+=*")


class SignerConfigError(ValueError):
    """Signer misconfigured. Messages name the variable, never its value."""


@dataclass(frozen=True)
class SignerConfig:
    kid: str
    key: bytes
    ttl_s: int


def load_signer_config(env: Mapping[str, str] = os.environ) -> SignerConfig | None:
    """None when neither key nor kid is set (signing not deployed);
    SignerConfigError when set but unusable."""
    raw_key = env.get(KEY_ENV, "").strip()
    kid = env.get(KID_ENV, "").strip()
    if not raw_key and not kid:
        return None
    if not _KID_RE.fullmatch(kid):
        raise SignerConfigError(f"{KID_ENV} missing or not [A-Za-z0-9_-]{{1,16}}")
    if not _KEY_RE.fullmatch(raw_key):
        raise SignerConfigError(f"{KEY_ENV} missing or not base64url")
    try:
        key = base64.urlsafe_b64decode(raw_key + "=" * (-len(raw_key) % 4))
    except ValueError:
        raise SignerConfigError(f"{KEY_ENV} does not decode") from None
    if len(key) < MIN_KEY_BYTES:
        raise SignerConfigError(f"{KEY_ENV} shorter than 256 bit")
    raw_ttl = env.get(TTL_ENV, "").strip() or str(DEFAULT_TTL_S)
    if not raw_ttl.isdigit() or int(raw_ttl) <= 0:
        raise SignerConfigError(f"{TTL_ENV} must be a positive integer (seconds)")
    return SignerConfig(kid=kid, key=key, ttl_s=int(raw_ttl))


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def sign(key: bytes, kid: str, exp: int, nonce: str, ticket_id: str) -> str:
    """Deterministic credential for fixed inputs (the vector-tested core)."""
    if not _TICKET_RE.fullmatch(ticket_id):
        raise ValueError("ticket id outside the credential's shape")
    payload = f"{VERSION}.{kid}.{exp}.{nonce}.{ticket_id}"
    version_key = hmac.new(key, _VERSION_LABEL, hashlib.sha256).digest()
    mac = hmac.new(version_key, payload.encode("ascii"), hashlib.sha256).digest()
    return f"{payload}.{_b64url(mac)}"


def issue(config: SignerConfig, ticket_id: str, now: float) -> str:
    """Fresh credential: 128-bit random nonce, `exp` = now + the signer TTL."""
    nonce = _b64url(secrets.token_bytes(16))
    return sign(config.key, config.kid, int(now) + config.ttl_s, nonce, ticket_id)
