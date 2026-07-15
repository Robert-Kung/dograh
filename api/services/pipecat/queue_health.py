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

import time
from collections.abc import Awaitable, Callable

from loguru import logger

DEFAULT_TIMEOUT_SECONDS = 0.5
DEFAULT_CACHE_TTL_SECONDS = 5.0

# url -> (expires_at_monotonic, healthy)
_cache: dict[str, tuple[float, bool]] = {}


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
    url = ((config or {}).get("queueHealthUrl") or "").strip()
    if not url:
        return True

    timeout = float(config.get("queueHealthTimeoutSeconds") or DEFAULT_TIMEOUT_SECONDS)
    ttl = float(config.get("queueHealthCacheTtlSeconds") or DEFAULT_CACHE_TTL_SECONDS)
    token = (config.get("queueHealthToken") or "").strip()

    now = monotonic()
    cached = _cache.get(url)
    if cached is not None and cached[0] > now:
        return cached[1]

    try:
        healthy = await (probe or _http_health)(url, token, timeout)
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
