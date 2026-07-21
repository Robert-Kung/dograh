"""Unified structured call events (S-L7-OBS).

One emit path for every call-lifecycle event — ``safetynet.*`` (published
contract, names and fields unchanged), ``transfer.ok`` / ``transfer.failed``,
``provider.error``, ``capacity.rejected`` (S-L9-SCALE; per-event fields
active/limit/outcome, no run id — the call never got one), and future
``consent.*`` / ``retention.*``. Writes the structured log line and hands the
event to the alert dispatcher.
"""

from loguru import logger

from api.services.observability import alerts


def emit(
    event: str,
    *,
    room_name: str,
    reason: str,
    workflow_run_id: int | None = None,
    elapsed_ms: int | None = None,
    **extra,
) -> None:
    fields = {
        "room_name": room_name,
        "reason": reason,
        "workflow_run_id": workflow_run_id,
        "elapsed_ms": elapsed_ms,
        **extra,
    }
    logger.bind(call_event=event, **fields).warning(
        f"{event} room={room_name} reason={reason} "
        f"workflow_run_id={workflow_run_id} elapsed_ms={elapsed_ms}"
    )
    alerts.notify(event, fields)
