"""Queue-health probe semantics (S-L5-QUEUE gate dimension).

Pure asyncio — the probe is injected, no network. Covers the MODIFIED
livekit-cold-transfer requirement's health scenarios: unset source is
unchecked, timeout/unreachable is unhealthy (fail-safe), TTL cache bounds
repeat probing.
"""

import pytest

from api.services.pipecat.queue_health import queue_is_healthy, reset_cache


@pytest.fixture(autouse=True)
def _fresh_cache():
    reset_cache()
    yield
    reset_cache()


async def test_unset_source_is_unchecked_and_healthy():
    calls = []

    async def probe(url, token, timeout):
        calls.append(url)
        return False

    assert await queue_is_healthy(None, probe=probe) is True
    assert await queue_is_healthy({}, probe=probe) is True
    assert await queue_is_healthy({"queueHealthUrl": "  "}, probe=probe) is True
    assert calls == []  # never probed


async def test_healthy_probe():
    async def probe(url, token, timeout):
        return True

    assert await queue_is_healthy({"queueHealthUrl": "http://q/h"}, probe=probe) is True


async def test_probe_timeout_is_unhealthy():
    async def probe(url, token, timeout):
        raise TimeoutError("connect timeout")

    assert (
        await queue_is_healthy({"queueHealthUrl": "http://q/h"}, probe=probe) is False
    )


async def test_probe_non_200_is_unhealthy():
    async def probe(url, token, timeout):
        return False

    assert (
        await queue_is_healthy({"queueHealthUrl": "http://q/h"}, probe=probe) is False
    )


async def test_ttl_cache_bounds_probing():
    clock = {"t": 100.0}
    calls = []

    async def probe(url, token, timeout):
        calls.append(clock["t"])
        return True

    config = {"queueHealthUrl": "http://q/h", "queueHealthCacheTtlSeconds": 5}
    for _ in range(3):  # within TTL: one probe only
        assert await queue_is_healthy(config, probe=probe, monotonic=lambda: clock["t"])
    assert len(calls) == 1

    clock["t"] += 6  # past TTL: re-probe
    assert await queue_is_healthy(config, probe=probe, monotonic=lambda: clock["t"])
    assert len(calls) == 2


async def test_unhealthy_result_also_cached():
    calls = []

    async def probe(url, token, timeout):
        calls.append(1)
        return False

    config = {"queueHealthUrl": "http://q/h"}
    assert await queue_is_healthy(config, probe=probe) is False
    assert await queue_is_healthy(config, probe=probe) is False
    assert len(calls) == 1  # negative verdict cached too (no probe storm mid-outage)


async def test_probe_receives_token_and_timeout():
    seen = {}

    async def probe(url, token, timeout):
        seen.update(url=url, token=token, timeout=timeout)
        return True

    await queue_is_healthy(
        {
            "queueHealthUrl": "http://q/internal/health",
            "queueHealthToken": "svc-secret",
            "queueHealthTimeoutSeconds": 0.3,
        },
        probe=probe,
    )
    assert seen == {
        "url": "http://q/internal/health",
        "token": "svc-secret",
        "timeout": 0.3,
    }
