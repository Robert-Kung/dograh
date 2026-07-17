"""Queue-health probe semantics (S-L5-QUEUE gate dimension).

Pure asyncio — the probe is injected, no network. Covers the MODIFIED
livekit-cold-transfer requirement's health scenarios: unset source is
unchecked, timeout/unreachable is unhealthy (fail-safe), TTL cache bounds
repeat probing.
"""

import asyncio

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


# --- config robustness (PR #8 review H1/F1/M2/M4) ---------------------------


async def test_malformed_numeric_config_degrades_never_raises():
    seen = {}

    async def probe(url, token, timeout):
        seen["timeout"] = timeout
        return True

    cfg = {
        "queueHealthUrl": "http://q/h",
        "queueHealthTimeoutSeconds": "0.5s",
        "queueHealthCacheTtlSeconds": "abc",
    }
    assert await queue_is_healthy(cfg, probe=probe) is True  # H1: no ValueError
    assert seen["timeout"] == 0.5  # defaults, not a crash


async def test_non_string_url_is_unchecked():
    async def probe(url, token, timeout):  # pragma: no cover
        raise AssertionError("must not probe")

    assert await queue_is_healthy({"queueHealthUrl": ["http://q"]}, probe=probe) is True


async def test_timeout_clamped_to_hard_cap():
    seen = {}

    async def probe(url, token, timeout):
        seen["timeout"] = timeout
        return True

    await queue_is_healthy(
        {"queueHealthUrl": "http://q/h", "queueHealthTimeoutSeconds": 30}, probe=probe
    )
    assert seen["timeout"] == 2.0  # F1: config cannot stretch the in-call pause


async def test_explicit_zero_ttl_disables_cache():
    calls = []

    async def probe(url, token, timeout):
        calls.append(url)
        return True

    cfg = {"queueHealthUrl": "http://q/h", "queueHealthCacheTtlSeconds": 0}
    await queue_is_healthy(cfg, probe=probe)
    await queue_is_healthy(cfg, probe=probe)
    assert len(calls) == 2  # M4: explicit 0 means "no cache", not the default


async def test_timeout_is_a_total_budget():
    async def stalling_probe(url, token, timeout):
        await asyncio.sleep(0.3)  # ignores the per-call timeout it was handed
        return True

    cfg = {"queueHealthUrl": "http://q/h", "queueHealthTimeoutSeconds": 0.05}
    assert await queue_is_healthy(cfg, probe=stalling_probe) is False  # M2: wall clock
