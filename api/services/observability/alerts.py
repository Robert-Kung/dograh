"""Key-event alerting over a configurable webhook (S-L7-OBS).

Immediate events fire one alert each; ``provider.error`` is aggregated in a
Redis sliding window so a flapping provider produces one summary instead of a
storm. Delivery is fire-and-forget: a broken webhook must never affect call
handling (C4 spirit). With ``OBS_ALERT_WEBHOOK_URL`` unset, alerting is
silently disabled — events still land in the structured log.

The payload is Slack incoming-webhook shaped (``{"text": ...}``), which most
webhook receivers accept.
"""

import os

from loguru import logger

from api.utils.background import spawn

IMMEDIATE_EVENTS = {
    "safetynet.triggered",
    "safetynet.transfer_failed",
    "safetynet.terminated",
    "transfer.failed",
}
WINDOWED_EVENTS = {"provider.error"}

_redis = None


def webhook_url() -> str | None:
    return (os.environ.get("OBS_ALERT_WEBHOOK_URL") or "").strip() or None


def window_seconds() -> int:
    return int(os.environ.get("OBS_ERROR_WINDOW_SECONDS", 300))


def threshold() -> int:
    return int(os.environ.get("OBS_ERROR_THRESHOLD", 3))


def log_alert_startup_status() -> None:
    if webhook_url() is None:
        logger.info(
            "OBS_ALERT_WEBHOOK_URL not set; call alerting disabled "
            "(structured events still logged)"
        )


def notify(event: str, fields: dict) -> None:
    """Route one structured event to the alert channel; cheap and never raises."""
    url = webhook_url()
    if url is None:
        return
    if event in IMMEDIATE_EVENTS:
        spawn(_send(url, _format(event, fields)))
    elif event in WINDOWED_EVENTS:
        spawn(_count_and_alert(url, event, fields))


def _format(event: str, fields: dict) -> str:
    parts = [f"[{event}]"]
    for key in ("room_name", "reason", "workflow_run_id", "elapsed_ms"):
        value = fields.get(key)
        if value is not None:
            parts.append(f"{key}={value}")
    return " ".join(parts)


async def _send(url: str, text: str) -> None:
    try:
        import httpx

        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json={"text": text})
    except Exception as e:
        logger.warning(f"alert webhook send failed: {e}")


async def _get_redis():
    global _redis
    if _redis is None:
        import redis.asyncio as aioredis

        _redis = aioredis.from_url(os.environ["REDIS_URL"])
    return _redis


async def _count_and_alert(url: str, event: str, fields: dict) -> None:
    try:
        r = await _get_redis()
        key = f"obs:alert:{event}"
        count = await r.incr(key)
        if count == 1:
            await r.expire(key, window_seconds())
        if count >= threshold():
            # Reset so the next burst starts a fresh window instead of
            # alerting on every subsequent occurrence.
            await r.delete(key)
            await _send(
                url,
                f"[{event}] {count} occurrences within {window_seconds()}s "
                f"(latest: {_format(event, fields)})",
            )
    except Exception as e:
        logger.warning(f"alert window counting failed: {e}")
