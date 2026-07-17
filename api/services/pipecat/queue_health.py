"""Queue-service health probe for the transfer gate (S-L5-QUEUE).

The business-hours gate gains a second dimension: 判斷 = 排程營運中 ∧ 隊列
服務健康. Semantics (spec `livekit-cold-transfer` MODIFIED delta):

- No health source configured -> unchecked; behavior identical to before the
  queue-health dimension existed.
- Configured but timing out / unreachable -> UNHEALTHY (fail-safe for C4: the
  caller stays with the AI and hears an explicit message, instead of being
  REFERred into a dead queue).
- A short TTL cache plus a hard timeout bound the added in-call latency.
- Staffing (online agent count) MUST NOT gate: zero-agent calls are REFERred
  and served by the queue's immediate-overflow callback path.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable

from loguru import logger

DEFAULT_TIMEOUT_SECONDS = 0.5
DEFAULT_CACHE_TTL_SECONDS = 5.0
# Hard caps: the probe sits on the in-call transfer path, so the spec's
# latency SHALL must survive any config value (review F1/M2/M4/H1 — config
# is free-form JSON with no schema; a typo must degrade, never raise or
# stretch the caller-audible pause).
MAX_TIMEOUT_SECONDS = 2.0
MAX_CACHE_TTL_SECONDS = 60.0

# url -> (expires_at_monotonic, healthy)
_cache: dict[str, tuple[float, bool]] = {}


def _bounded_seconds(raw, default: float, maximum: float) -> float:
    """Tolerant numeric config: None -> default, junk/negative -> default
    (fail-safe, never raises — C4), always clamped to the hard cap. An
    explicit 0 is honored (TTL 0 disables the cache) instead of being
    swallowed by ``or``."""
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            f"invalid queue-health numeric config {raw!r}; using default {default}"
        )
        return default
    if value < 0:
        return default
    return min(value, maximum)


def _text(raw) -> str:
    return raw.strip() if isinstance(raw, str) else ""


async def _http_health(url: str, token: str, timeout: float) -> bool:
    import httpx

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url, headers=headers)
        return response.status_code == 200


async def queue_is_healthy(
    config: dict | None,
    *,
    probe: Callable[[str, str, float], Awaitable[bool]] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> bool:
    """Health verdict for the transfer gate; True when unconfigured.

    Config keys (tool config, org/workflow level):
      queueHealthUrl / queueHealthToken /
      queueHealthTimeoutSeconds / queueHealthCacheTtlSeconds
    """
    url = _text((config or {}).get("queueHealthUrl"))
    if not url:
        return True

    timeout = _bounded_seconds(
        config.get("queueHealthTimeoutSeconds"),
        DEFAULT_TIMEOUT_SECONDS,
        MAX_TIMEOUT_SECONDS,
    )
    ttl = _bounded_seconds(
        config.get("queueHealthCacheTtlSeconds"),
        DEFAULT_CACHE_TTL_SECONDS,
        MAX_CACHE_TTL_SECONDS,
    )
    token = _text(config.get("queueHealthToken"))

    now = monotonic()
    cached = _cache.get(url)
    if cached is not None and cached[0] > now:
        return cached[1]

    try:
        # wait_for makes the timeout a TOTAL budget — the httpx timeout alone
        # is per-phase (connect/read/write each), up to ~4x in the worst case
        # (review M2: "hard timeout" must mean wall clock).
        healthy = await asyncio.wait_for(
            (probe or _http_health)(url, token, timeout), timeout=timeout
        )
    except Exception as e:
        logger.warning(f"queue health probe failed ({url}): {e}; treating as unhealthy")
        healthy = False
    if not healthy:
        logger.warning(f"queue service unhealthy ({url}); transfer gate will hold")
    _cache[url] = (now + ttl, healthy)
    return healthy


def reset_cache() -> None:
    """Test hook."""
    _cache.clear()
