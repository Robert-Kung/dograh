"""Call outcome tagging for trace replay (S-L7-OBS).

Every LIVEKIT call ends as exactly one of ``ai_completed`` /
``transferred:<reason>`` / ``transfer_failed:<reason>`` /
``safetynet_terminated``, written to the current OTel span (Langfuse
filtering) and the workflow_run annotations (queryable without a trace).

Precedence: a terminal success (``transferred``/``safetynet_terminated``)
overwrites an earlier ``transfer_failed`` (e.g. a failed press-0 followed by a
successful voice transfer); ``ai_completed`` only applies when nothing else
was recorded. Never raises — observability must not break call handling.
"""

from loguru import logger

_RANKS = {
    "ai_completed": 0,
    "transfer_failed": 1,
    "transferred": 2,
    "safetynet_terminated": 2,
}


def _rank(outcome: str) -> int:
    return _RANKS.get(outcome.split(":", 1)[0], 1)


async def _persisted_outcome(workflow_run_id: int) -> str | None:
    """The run's stored call_outcome — one column, no joined workflow/user."""
    from sqlalchemy import select

    from api.db import db_client
    from api.db.models import WorkflowRunModel

    async with db_client.async_session() as session:
        annotations = (
            await session.execute(
                select(WorkflowRunModel.annotations).where(
                    WorkflowRunModel.id == workflow_run_id
                )
            )
        ).scalar_one_or_none()
    return (annotations or {}).get("call_outcome")


async def record_call_outcome(
    engine,
    workflow_run_id: int | None,
    *,
    outcome: str,
    transfer_reason: str | None = None,
) -> None:
    try:
        previous = getattr(engine, "_call_outcome", None) if engine else None
        if previous is None and workflow_run_id is not None:
            # Nothing in memory: an engine-free path (server-side safetynet)
            # may already have persisted a terminal outcome this engine never
            # saw — read it back so a late ai_completed can't overwrite it.
            # A failed read must not lose this outcome (W4b review 8.1 M2):
            # write as before rather than record nothing.
            try:
                previous = await _persisted_outcome(workflow_run_id)
            except Exception as e:
                logger.warning(
                    f"call outcome read-back failed for run {workflow_run_id}: "
                    f"{type(e).__name__}"
                )
        if previous is not None and _rank(outcome) <= _rank(previous):
            return
        if engine is not None:
            engine._call_outcome = outcome

        from opentelemetry import trace as otel_trace

        span = otel_trace.get_current_span()
        if span is not None and span.is_recording():
            span.set_attribute("dograh.call_outcome", outcome)
            if transfer_reason:
                span.set_attribute("dograh.transfer_reason", transfer_reason)

        if workflow_run_id is not None:
            from api.db import db_client

            annotations = {"call_outcome": outcome}
            if transfer_reason:
                annotations["transfer_reason"] = transfer_reason
            await db_client.update_workflow_run(
                workflow_run_id, annotations=annotations
            )
    except Exception as e:
        logger.warning(f"record_call_outcome failed for run {workflow_run_id}: {e}")


async def record_call_fact(workflow_run_id: int | None, **facts) -> None:
    """Write per-call facts that are *not* the outcome (ccp#7 review #1).

    ``call_outcome`` is one slot with rank precedence, so two facts recorded
    at setup — press-0 not installed, safetynet not installed — competed for
    it and the second was dropped. A fact about the call's *protection* is
    orthogonal to how the call *ended*: it goes under its own annotation keys
    (merged by ``update_workflow_run``, so a later outcome write keeps it) and
    onto the span as ``dograh.<key>``. No precedence, no engine state: a
    repeated call writes again and the last value wins. Never raises.
    """
    try:
        from opentelemetry import trace as otel_trace

        span = otel_trace.get_current_span()
        if span is not None and span.is_recording():
            for key, value in facts.items():
                span.set_attribute(f"dograh.{key}", value)

        if workflow_run_id is not None:
            from api.db import db_client

            await db_client.update_workflow_run(workflow_run_id, annotations=facts)
    except Exception as e:
        logger.warning(f"record_call_fact failed for run {workflow_run_id}: {e}")
