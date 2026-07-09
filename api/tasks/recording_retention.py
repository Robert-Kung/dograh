"""PDPA retention sweep for call recordings and transcripts (S-L8-RECORD).

Daily cron mirroring :mod:`api.tasks.ticket_retention`: delete storage objects
(mixed + per-track recordings, transcript) for runs older than
``RECORD_RETENTION_DAYS``, clear the columns, and leave an insert-only audit
row per run. Idempotent — a cleared ``recording_url`` never matches again;
per-run failures are logged and re-picked on the next sweep. The consent
record in workflow_run annotations is compliance evidence and is never
touched here.
"""

from loguru import logger

from api.db import db_client
from api.services.pipecat.livekit_consent import retention_days
from api.services.storage import get_storage_for_backend
from api.utils.recording_artifacts import get_recording_storage_key


def log_retention_event(
    workflow_run_id: int, object_keys: list[str], days: int
) -> None:
    """Structured ``retention.recording_deleted`` event via the unified path."""
    from api.services.observability.call_events import emit

    emit(
        "retention.recording_deleted",
        room_name="",
        reason=f"retention_days={days}",
        workflow_run_id=workflow_run_id,
        object_keys=object_keys,
    )


def _object_keys(run) -> list[str]:
    keys = [run.recording_url]
    for track in ("user", "bot"):
        key = get_recording_storage_key(run.extra, track)
        if key:
            keys.append(key)
    if run.transcript_url:
        keys.append(run.transcript_url)
    return [k for k in keys if k]


async def enforce_recording_retention(_ctx) -> None:
    days = retention_days()
    runs = await db_client.get_expired_recording_runs(days)
    if not runs:
        return
    deleted = 0
    for run in runs:
        keys = _object_keys(run)
        try:
            fs = get_storage_for_backend(run.storage_backend)
            failures = []
            for key in keys:
                if not await fs.adelete_file(key):
                    failures.append(key)
            if failures:
                raise RuntimeError(f"storage delete failed for {failures}")
            await db_client.clear_recording_artifacts(run.id)
            await db_client.create_recording_retention_audit(
                run.id, object_keys=keys, retention_days=days, result="ok"
            )
            log_retention_event(run.id, keys, days)
            deleted += 1
        except Exception as e:
            # Leave the row intact — recording_url still set means the next
            # sweep retries. The audit row records the failed attempt.
            logger.error(f"recording retention failed for run {run.id}: {e}")
            await db_client.create_recording_retention_audit(
                run.id,
                object_keys=keys,
                retention_days=days,
                result=f"failed: {str(e)[:200]}",
            )
    if deleted:
        logger.info(
            f"recording_retention: deleted recordings for {deleted} runs "
            f"(retention_days={days})"
        )
