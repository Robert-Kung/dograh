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


async def record_call_outcome(
    engine,
    workflow_run_id: int | None,
    *,
    outcome: str,
    transfer_reason: str | None = None,
) -> None:
    try:
        previous = getattr(engine, "_call_outcome", None) if engine else None
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
