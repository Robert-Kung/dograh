"""PDPA retention sweeps for call recordings and transcripts (S-L8-RECORD, ccp W4c).

Daily cron mirroring :mod:`api.tasks.ticket_retention`, two sweeps on separate
settings:

- audio — ``RECORD_AUDIO_RETENTION_DAYS``: mixed and per-track recordings;
- transcript — ``RECORD_TRANSCRIPT_RETENTION_DAYS`` (days or ``never``): the
  transcript object, its DB copy, the caller number's derived data and the
  extracted values.

Each deletion leaves an insert-only audit row with its scope. Idempotent — a
cleared row never matches again; per-run failures are logged and re-picked on
the next sweep. The consent record in workflow_run annotations is compliance
evidence and is never touched here.
"""

from loguru import logger

from api.db import db_client
from api.services.pipecat.livekit_consent import (
    NEVER,
    audio_retention_days,
    transcript_retention,
)
from api.services.storage import get_storage_for_backend
from api.utils.recording_artifacts import get_recording_storage_key


def log_retention_event(
    workflow_run_id: int, object_keys: list[str], days: int, scope: str
) -> None:
    """Structured ``retention.recording_deleted`` event via the unified path."""
    from api.services.observability.call_events import emit

    emit(
        "retention.recording_deleted",
        room_name="",
        reason=f"scope={scope} retention_days={days}",
        workflow_run_id=workflow_run_id,
        object_keys=object_keys,
    )


def _audio_keys(run) -> list[str]:
    keys = [run.recording_url]
    for track in ("user", "bot"):
        keys.append(get_recording_storage_key(run.extra, track))
    return [k for k in keys if k]


def _transcript_keys(run) -> list[str]:
    return [run.transcript_url] if run.transcript_url else []


BATCH = 500
# A day's sweep stops after this many batches (500 000 runs) and resumes the
# next day; failed runs are passed over by id so they cannot hold a batch.
MAX_BATCHES = 1000


async def _sweep(scope: str, days: int, fetch, keys_of, clear) -> int:
    deleted, after_id = 0, 0
    for _ in range(MAX_BATCHES):
        runs = await fetch(days, limit=BATCH, after_id=after_id)
        for run in runs:
            if await _expire(scope, days, run, keys_of(run), clear):
                deleted += 1
        if len(runs) < BATCH:
            break
        after_id = runs[-1].id
    if deleted:
        logger.info(
            f"recording_retention: {scope} deleted for {deleted} runs "
            f"(retention_days={days})"
        )
    return deleted


async def _expire(scope: str, days: int, run, keys: list[str], clear) -> bool:
    try:
        # nothing in storage (DB copy, number, extracted values only): no
        # backend to resolve
        if keys:
            fs = get_storage_for_backend(run.storage_backend)
            failures = [key for key in keys if not await fs.adelete_file(key)]
            if failures:
                raise RuntimeError(f"storage delete failed for {len(failures)} objects")
        await clear(run.id)
        await db_client.create_recording_retention_audit(
            run.id, object_keys=keys, retention_days=days, result="ok", scope=scope
        )
        log_retention_event(run.id, keys, days, scope)
        return True
    except Exception as e:
        # Leave the row intact — still pending means the next sweep retries.
        # Type only: a DB error's text quotes bound parameters (logs,
        # gathered_context) into an insert-only table no retention reaches.
        reason = type(e).__name__
        logger.error(f"{scope} retention failed for run {run.id}: {reason}")
        await db_client.create_recording_retention_audit(
            run.id,
            object_keys=keys,
            retention_days=days,
            result=f"failed: {reason}",
            scope=scope,
        )
        return False


async def enforce_recording_retention(_ctx) -> None:
    days = audio_retention_days()
    await _sweep(
        "audio",
        days,
        db_client.get_expired_audio_runs,
        _audio_keys,
        db_client.clear_audio_artifacts,
    )

    setting = transcript_retention()
    if setting is None:
        # preflight enforces this; at runtime dograh keeps running (C4)
        logger.error(
            "RECORD_TRANSCRIPT_RETENTION_DAYS unset or invalid; "
            "transcript retention skipped"
        )
        return
    if setting == NEVER:
        return
    await _sweep(
        "transcript",
        setting,
        db_client.get_expired_transcript_runs,
        _transcript_keys,
        db_client.clear_transcript_artifacts,
    )
