"""Shared cold-transfer preamble (S-L3-PRESS0).

Single funnel for both cold-transfer triggers — the LLM voice tool and the
press-0 DTMF gate — so the business-hours gate, after-hours behavior, and the
double-REFER guard are defined once instead of per trigger.

Split into:

- :func:`plan_transfer` — a pure decision (open → REFER, else the configured
  after-hours action with unknown values falling back to the default). Fully
  unit-testable without an engine.
- :func:`execute_cold_transfer` — the engine-side executor that runs the plan:
  idempotency guard, the optional pre-REFER announcement, the SIP REFER, or the
  after-hours announcement/hangup. Every path returns a structured result and
  never raises (C4).

The idempotency guard lives here, at the shared entry both triggers pass
through, rather than inside ``cold_transfer_to_human`` (which stays a clean SIP
primitive): this is exactly where a near-simultaneous voice + press-0 race
would otherwise issue two REFERs.
"""

import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from enum import Enum

from api.services.pipecat.business_hours import is_open

logger = logging.getLogger(__name__)

# The pipecat frame / enum and the SIP REFER primitive are imported lazily inside
# the executor so the pure decision logic (plan_transfer / valid_destination)
# stays importable — and unit-testable — without the pipecat + livekit runtime.

# Config-driven destination: tel:+E164 or sip:user@host. Never caller input (C6).
_DESTINATION_RE = re.compile(r"^(tel:\+[1-9]\d{1,14}|sip:[\w\-.@:]+)$")

DEFAULT_AFTER_HOURS_ACTION = "back_to_ai"
_SUPPORTED_AFTER_HOURS = {"back_to_ai", "announce_and_hangup", "alternate_queue"}
_DEFAULT_AFTER_HOURS_MESSAGE = "目前為非營運時間，將由 AI 繼續為您服務。"
_DEFAULT_UNAVAILABLE_MESSAGE = "轉接服務暫時不可用，將由 AI 繼續為您服務。"
_DEFAULT_UNAVAILABLE_HANGUP_MESSAGE = "轉接服務暫時不可用，請稍後再撥，感謝您的來電。"
DEFAULT_UNAVAILABLE_ANNOUNCE_LIMIT = 2


class TransferDecision(str, Enum):
    """Resolved branch for a transfer trigger."""

    REFER = "refer"
    AFTER_HOURS_BACK_TO_AI = "back_to_ai"
    AFTER_HOURS_HANGUP = "announce_and_hangup"
    AFTER_HOURS_ALTERNATE = "alternate_queue"
    # Queue service unhealthy (S-L5-QUEUE): distinct from after-hours — the
    # schedule says open, but REFERring would land the caller in a dead queue.
    UNAVAILABLE = "transfer_unavailable"


def valid_destination(destination: str | None) -> bool:
    """True if ``destination`` is a well-formed, config-shaped REFER target."""
    return bool(destination and _DESTINATION_RE.match(destination.strip()))


def plan_transfer(
    schedule: dict | None,
    after_hours_action: str | None,
    now: datetime,
    queue_healthy: bool = True,
) -> TransferDecision:
    """Decide the transfer branch: 排程 ∧ 隊列健康 (S-L5-QUEUE).

    Out of hours -> the configured after-hours action (unchanged; unknown values
    fall back to the default, C4). In hours but queue unhealthy -> UNAVAILABLE
    (independent handling, not after-hours). Staffing is deliberately not an
    input — zero-agent calls REFER and hit the queue's immediate overflow.
    """
    if is_open(schedule, now):
        return TransferDecision.REFER if queue_healthy else TransferDecision.UNAVAILABLE

    action = (
        after_hours_action
        if after_hours_action in _SUPPORTED_AFTER_HOURS
        else (DEFAULT_AFTER_HOURS_ACTION)
    )
    return {
        "back_to_ai": TransferDecision.AFTER_HOURS_BACK_TO_AI,
        "announce_and_hangup": TransferDecision.AFTER_HOURS_HANGUP,
        "alternate_queue": TransferDecision.AFTER_HOURS_ALTERNATE,
    }[action]


async def _do_refer(
    engine, room_name: str, destination: str, lk, transfer_reason: str = "unknown"
) -> dict:
    """Issue the SIP REFER; on success end the call, on failure stay structured (C4, §3.5).

    S-L4 handoff wraps the REFER here: ticket id into the REFER headers,
    skeleton write in the background before it, summary job enqueued after
    the outcome is known. An unconfigured or failing handoff changes nothing
    about the transfer itself (C4/D5).
    """
    from api.services.observability.call_events import emit
    from api.services.observability.call_outcome import record_call_outcome
    from api.services.pipecat.livekit_cold_transfer import cold_transfer_to_human
    from api.services.pipecat.transfer_context_handoff import (
        finalize_transfer_handoff,
        prepare_transfer_handoff,
    )
    from pipecat.utils.enums import EndTaskReason
    from pipecat.utils.run_context import get_current_run_id

    plan = await prepare_transfer_handoff(
        engine, room_name=room_name, transfer_reason=transfer_reason, lk=lk
    )

    result = await cold_transfer_to_human(
        room_name,
        destination,
        headers=plan.refer_headers if plan else None,
        lk=lk,
    )
    if plan is not None:
        await finalize_transfer_handoff(plan, result.get("status", "failed"))

    # S-L7-OBS: every cold transfer — voice tool, press-0, safetynet — funnels
    # through here, so this is the single emission point for transfer events
    # and the call-outcome tag.
    try:
        run_id = get_current_run_id()
        workflow_run_id = int(run_id) if run_id is not None else None
    except (TypeError, ValueError):
        workflow_run_id = None
    succeeded = result.get("status") == "success"
    emit(
        "transfer.ok" if succeeded else "transfer.failed",
        room_name=room_name,
        reason=transfer_reason if succeeded else result.get("reason", "unknown"),
        workflow_run_id=workflow_run_id,
        transfer_reason=transfer_reason,
    )
    await record_call_outcome(
        engine,
        workflow_run_id,
        outcome=(
            f"transferred:{transfer_reason}"
            if succeeded
            else f"transfer_failed:{transfer_reason}"
        ),
        transfer_reason=transfer_reason,
    )

    if succeeded:
        await engine.end_call_with_reason(
            EndTaskReason.TRANSFER_CALL.value, abort_immediately=False
        )
    # REFER failure: no auto-retry — caller hands the structured result back to
    # the LLM and announces a fallback.
    return result


async def _announce_unavailable(engine, message: str | None, limit: int | None) -> dict:
    """Queue unhealthy: explicit message back to the AI, bounded per call.

    The per-call announcement counter lives on the engine (like the REFER
    idempotency flag); once past the limit the call ends explicitly so a
    persistent outage can't loop the caller AI ↔ gate forever (C4).
    """
    from pipecat.frames.frames import TTSSpeakFrame
    from pipecat.utils.enums import EndTaskReason

    cap = DEFAULT_UNAVAILABLE_ANNOUNCE_LIMIT if limit is None else int(limit)
    count = getattr(engine, "_transfer_unavailable_announcements", 0) + 1
    engine._transfer_unavailable_announcements = count

    if count > cap:
        await engine.task.queue_frame(
            TTSSpeakFrame(_DEFAULT_UNAVAILABLE_HANGUP_MESSAGE, persist_to_logs=True)
        )
        await engine.end_call_with_reason(
            EndTaskReason.END_CALL_TOOL_REASON.value, abort_immediately=False
        )
        return {"status": "unavailable", "action": "announced_hangup"}

    await engine.task.queue_frame(
        TTSSpeakFrame(message or _DEFAULT_UNAVAILABLE_MESSAGE, persist_to_logs=True)
    )
    return {"status": "unavailable", "action": "back_to_ai"}


async def execute_cold_transfer(
    engine,
    *,
    room_name: str,
    destination: str,
    schedule: dict | None = None,
    after_hours_action: str | None = None,
    alternate_destination: str | None = None,
    after_hours_message: str | None = None,
    before_refer: Callable[[], Awaitable[None]] | None = None,
    now: datetime | None = None,
    lk=None,
    transfer_reason: str = "unknown",
    queue_health_config: dict | None = None,
    unavailable_message: str | None = None,
    unavailable_announce_limit: int | None = None,
) -> dict:
    """Run the shared cold-transfer flow and return a structured result (never raises).

    Args:
        engine: Pipecat engine; uses ``task.queue_frame``, ``end_call_with_reason``,
            and an ``_livekit_transfer_in_progress`` idempotency flag.
        room_name: LiveKit room of the live call.
        destination: Primary (business-hours) REFER target, config-validated.
        schedule: Weekly business-hours schedule (see :mod:`business_hours`).
        after_hours_action: ``back_to_ai`` (default) | ``announce_and_hangup`` |
            ``alternate_queue``; unknown values fall back to the default.
        alternate_destination: Night/voicemail queue for ``alternate_queue``.
        after_hours_message: Spoken when not transferring; a default is used if unset.
        before_refer: Optional announcement awaited just before a REFER (the voice
            path plays its configured transfer message here).
        now: Evaluation instant; defaults to current UTC.
        lk: Optional injected LiveKitAPI (tests).
        transfer_reason: Trigger label recorded on the handoff ticket
            ("voice_tool" | "press0"); never caller-derived.
        queue_health_config: Tool-config dict holding the queue-health keys
            (see :mod:`queue_health`). None/unset -> no health check (behavior
            identical to before S-L5-QUEUE). The safetynet path deliberately
            never passes this — it REFERs regardless (documented interaction).
        unavailable_message: Spoken when the queue is unhealthy (back-to-AI).
        unavailable_announce_limit: Per-call cap on unavailable announcements;
            once exceeded the call is ended explicitly instead of looping
            AI ↔ gate forever (C4, default 2).
    """
    if not valid_destination(destination):
        # Pre-flight failure never reaches _do_refer — emit here so a config
        # typo still alerts and tags the call (S-L7-OBS H2).
        from api.services.observability.call_events import emit
        from api.services.observability.call_outcome import record_call_outcome
        from pipecat.utils.run_context import get_current_run_id

        try:
            run_id = get_current_run_id()
            workflow_run_id = int(run_id) if run_id is not None else None
        except (TypeError, ValueError):
            workflow_run_id = None
        emit(
            "transfer.failed",
            room_name=room_name,
            reason="invalid_destination",
            workflow_run_id=workflow_run_id,
            transfer_reason=transfer_reason,
        )
        await record_call_outcome(
            engine,
            workflow_run_id,
            outcome=f"transfer_failed:{transfer_reason}",
            transfer_reason=transfer_reason,
        )
        return {
            "status": "failed",
            "action": "transfer_failed",
            "reason": "invalid_destination",
        }

    if getattr(engine, "_livekit_transfer_in_progress", False):
        return {
            "status": "failed",
            "action": "transfer_failed",
            "reason": "already_transferring",
        }
    engine._livekit_transfer_in_progress = True
    try:
        from api.services.pipecat.queue_health import queue_is_healthy

        decision = plan_transfer(
            schedule,
            after_hours_action,
            now or datetime.now(timezone.utc),
            queue_healthy=await queue_is_healthy(queue_health_config),
        )

        if decision is TransferDecision.UNAVAILABLE:
            return await _announce_unavailable(
                engine, unavailable_message, unavailable_announce_limit
            )

        if decision is TransferDecision.AFTER_HOURS_ALTERNATE:
            if valid_destination(alternate_destination):
                # A night queue is still a human pickup — same handoff.
                return await _do_refer(
                    engine, room_name, alternate_destination, lk, transfer_reason
                )
            # Configured for alternate queue but no valid target — fall back to
            # keeping the caller with the AI rather than dropping them (C4).
            logger.warning(
                "alternate_queue selected but alternate_destination invalid; back_to_ai"
            )
            decision = TransferDecision.AFTER_HOURS_BACK_TO_AI

        if decision is TransferDecision.REFER:
            if before_refer is not None:
                await before_refer()
            return await _do_refer(engine, room_name, destination, lk, transfer_reason)

        # After-hours announce branches.
        from pipecat.frames.frames import TTSSpeakFrame
        from pipecat.utils.enums import EndTaskReason

        await engine.task.queue_frame(
            TTSSpeakFrame(
                after_hours_message or _DEFAULT_AFTER_HOURS_MESSAGE,
                persist_to_logs=True,
            )
        )
        if decision is TransferDecision.AFTER_HOURS_HANGUP:
            await engine.end_call_with_reason(
                EndTaskReason.END_CALL_TOOL_REASON.value, abort_immediately=False
            )
            return {"status": "after_hours", "action": "announced_hangup"}
        return {"status": "after_hours", "action": "back_to_ai"}
    finally:
        engine._livekit_transfer_in_progress = False
