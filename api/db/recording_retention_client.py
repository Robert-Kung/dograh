"""DB access for recording and transcript retention (S-L8-RECORD, ccp W4c).

``update_workflow_run``'s truthy guards can't null a column, so clearing
artifacts needs its own writers. The audit table is insert-only.

Audio and transcript expire on separate settings (ccp W4c 設計 G). A transcript
lives in more places than its object: the DB copy in
``logs.realtime_feedback_events``, the caller number's mask and HMAC in
``ccp_call_meta``, and extracted values in ``gathered_context`` — all cleared
together.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text, update

from api.db.base_client import BaseDBClient
from api.db.models import (
    CcpCallMetaModel,
    RecordingRetentionAuditModel,
    WorkflowRunModel,
)

# gathered_context keys a transcript expiry keeps: system-written, carrying no
# caller content. Extracted values are also copied to the top level and into
# call_disposition / call_tags, so everything else goes (task 0.2).
TRANSCRIPT_KEEP_KEYS = (
    "mapped_call_disposition",  # usage-report category (W4b)
    "nodes_visited",
    "trace_url",
    "call_id",
    "transfer_state",
    "answered_by",
    "error",
)

_AUDIO_PENDING = """(
    recording_url IS NOT NULL
    OR (json_typeof(extra) = 'object' AND extra->'recordings' IS NOT NULL)
)"""

# logs / gathered_context are JSON, not JSONB: a full scan, batched (R-AW).
_TRANSCRIPT_PENDING = """(
    transcript_url IS NOT NULL
    OR (json_typeof(logs) = 'object' AND logs->'realtime_feedback_events' IS NOT NULL)
    OR EXISTS (
        SELECT 1 FROM ccp_call_meta m
        WHERE m.workflow_run_id = workflow_runs.id
          AND (m.caller_hmac IS NOT NULL OR m.caller_last4 IS NOT NULL
               OR m.caller_masked IS NOT NULL)
    )
    OR EXISTS (
        -- CASE, not AND: Postgres need not evaluate the type check first
        SELECT 1 FROM json_object_keys(
            CASE WHEN json_typeof(gathered_context) = 'object'
                 THEN gathered_context ELSE '{}'::json END) AS k
        WHERE k <> ALL(CAST(:keep AS text[]))
    )
)"""


class RecordingRetentionClient(BaseDBClient):
    async def _expired_runs(
        self,
        pending: str,
        days: int,
        limit: int,
        params: dict | None = None,
        after_id: int = 0,
    ) -> list[WorkflowRunModel]:
        # Anchored on created_at (call start): the model has no ended-at
        # column, and call start is always <= call end, so this errs early.
        cutoff = datetime.now(UTC) - timedelta(days=days)
        async with self.async_session() as session:
            result = await session.execute(
                select(WorkflowRunModel)
                .where(WorkflowRunModel.created_at < cutoff)
                .where(WorkflowRunModel.id > after_id)
                .where(text(pending).bindparams(**(params or {})))
                .order_by(WorkflowRunModel.id)
                .limit(limit)
            )
            return list(result.scalars().all())

    async def get_expired_audio_runs(
        self, retention_days: int, limit: int = 500, after_id: int = 0
    ) -> list[WorkflowRunModel]:
        """Runs still holding a mixed or per-track recording past the window.

        Cleared rows never match again; failed rows are re-picked next sweep.
        """
        return await self._expired_runs(
            _AUDIO_PENDING, retention_days, limit, after_id=after_id
        )

    async def get_expired_transcript_runs(
        self, retention_days: int, limit: int = 500, after_id: int = 0
    ) -> list[WorkflowRunModel]:
        """Runs past the window still holding any transcript-derived data.

        Every mode — an editor test call's transcript expires too.
        """
        return await self._expired_runs(
            _TRANSCRIPT_PENDING,
            retention_days,
            limit,
            {"keep": list(TRANSCRIPT_KEEP_KEYS)},
            after_id=after_id,
        )

    async def clear_audio_artifacts(self, workflow_run_id: int) -> None:
        """Null ``recording_url`` and drop per-track metadata."""
        async with self.async_session() as session:
            run = await self._locked(session, workflow_run_id)
            if not run:
                return
            run.recording_url = None
            extra = dict(run.extra or {})
            extra.pop("recordings", None)
            run.extra = extra
            await session.commit()

    async def clear_transcript_artifacts(self, workflow_run_id: int) -> None:
        """Null ``transcript_url``, drop the DB copy, number and extracted values.

        ``audio_started_at`` stays: it is the recording's time origin.
        """
        async with self.async_session() as session:
            run = await self._locked(session, workflow_run_id)
            if not run:
                return
            run.transcript_url = None
            if isinstance(run.logs, dict):
                logs = dict(run.logs)
                logs.pop("realtime_feedback_events", None)
                run.logs = logs
            if isinstance(run.gathered_context, dict):
                run.gathered_context = {
                    k: v
                    for k, v in run.gathered_context.items()
                    if k in TRANSCRIPT_KEEP_KEYS
                }
            await session.execute(
                update(CcpCallMetaModel)
                .where(CcpCallMetaModel.workflow_run_id == workflow_run_id)
                .values(caller_masked=None, caller_last4=None, caller_hmac=None)
            )
            await session.commit()

    @staticmethod
    async def _locked(session, workflow_run_id: int) -> WorkflowRunModel | None:
        result = await session.execute(
            select(WorkflowRunModel)
            .where(WorkflowRunModel.id == workflow_run_id)
            .with_for_update()
        )
        return result.scalars().first()

    async def create_recording_retention_audit(
        self,
        workflow_run_id: int,
        *,
        object_keys: list[str],
        retention_days: int,
        result: str,
        scope: str,
    ) -> None:
        async with self.async_session() as session:
            session.add(
                RecordingRetentionAuditModel(
                    workflow_run_id=workflow_run_id,
                    object_keys=object_keys,
                    retention_days=retention_days,
                    result=result,
                    scope=scope,
                )
            )
            await session.commit()
