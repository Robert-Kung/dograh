"""DB access for recording retention (S-L8-RECORD).

``update_workflow_run``'s truthy guards can't null a column, so clearing
recording artifacts needs its own writer. The audit table is insert-only.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from api.db.base_client import BaseDBClient
from api.db.models import RecordingRetentionAuditModel, WorkflowRunModel


class RecordingRetentionClient(BaseDBClient):
    async def get_expired_recording_runs(
        self, retention_days: int, limit: int = 500
    ) -> list[WorkflowRunModel]:
        """Runs still holding a recording older than the retention window.

        Anchored on ``created_at`` (call start) — the model has no ended-at
        column, and call start is always ≤ call end, so this errs conservative.
        ``recording_url`` doubles as the idempotency marker: cleared rows never
        match again, and failed rows are re-picked on the next sweep.
        """
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        async with self.async_session() as session:
            result = await session.execute(
                select(WorkflowRunModel)
                .where(WorkflowRunModel.recording_url.isnot(None))
                .where(WorkflowRunModel.created_at < cutoff)
                .order_by(WorkflowRunModel.id)
                .limit(limit)
            )
            return list(result.scalars().all())

    async def clear_recording_artifacts(self, workflow_run_id: int) -> None:
        """Null the recording/transcript columns and drop track metadata."""
        async with self.async_session() as session:
            result = await session.execute(
                select(WorkflowRunModel)
                .where(WorkflowRunModel.id == workflow_run_id)
                .with_for_update()
            )
            run = result.scalars().first()
            if not run:
                return
            run.recording_url = None
            run.transcript_url = None
            extra = dict(run.extra or {})
            extra.pop("recordings", None)
            run.extra = extra
            await session.commit()

    async def create_recording_retention_audit(
        self,
        workflow_run_id: int,
        *,
        object_keys: list[str],
        retention_days: int,
        result: str,
    ) -> None:
        async with self.async_session() as session:
            session.add(
                RecordingRetentionAuditModel(
                    workflow_run_id=workflow_run_id,
                    object_keys=object_keys,
                    retention_days=retention_days,
                    result=result,
                )
            )
            await session.commit()
