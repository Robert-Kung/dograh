"""AI-concurrency capacity gate (S-L9-SCALE, first vertical slice).

Admission for LIVEKIT inbound calls: the dispatcher makes a synchronous
check-and-reserve decision (primitives in :mod:`active_calls`) before creating
any run, and hands full-capacity calls to :func:`capacity_overflow` — a
background action chain that REFERs the caller straight to the human queue,
or deletes the room so the call ends explicitly (C4, never dead air).

Config is env-only (C6 — callers can never influence the target) and
validated at startup via :func:`validate_capacity_config`, wired into the
``app.py`` lifespan next to ``validate_safetynet_config``: a malformed
overflow target must stop boot, not surface on the first full-capacity call.
"""

import asyncio
import os
from datetime import datetime, timezone

from loguru import logger

from api.services.observability import call_events
from api.services.pipecat.livekit_transfer_flow import (
    TransferDecision,
    resolve_transfer_decision,
    valid_destination,
)

DEFAULT_MAX_CONCURRENT_CALLS = 6
# Flood valve (security F4): beyond this many in-flight overflow actions the
# chain skips polling/REFER and deletes the room outright, bounding the
# fan-out of poll + REFER work a burst can pin on the process.
DEFAULT_MAX_INFLIGHT_OVERFLOW = 8

# Premium-rate destination prefixes rejected at startup (C6). The overflow
# target auto-REFERs every caller while the gate is full, so a poisoned or
# fat-fingered value has toll-fraud blast radius beyond what shape validation
# catches. A guard, not an allowlist: 1-900/1-976 (NANP premium) and 886-204
# (Taiwan premium voice). Digit form, compared after stripping any leading
# ``+``, so a sip user expressing the number without ``+`` is caught too —
# the cost is that a bare PBX extension like ``sip:1900@pbx`` is refused at
# boot and needs renaming, a loud and cheap failure next to silent toll fraud.
PREMIUM_RATE_PREFIXES = ("1900", "1976", "886204")

# Wall-clock bound on one overflow action chain (gate probe ≤2s + poll ≤3s +
# REFER + delete). A hung LiveKit call must not pin its room in the guard set
# and its flood-valve slot forever.
OVERFLOW_ACTION_TIMEOUT_SECONDS = 15.0

# Rooms with an overflow action currently in flight. An in-progress guard, NOT
# a permanent fired-set (security F1): room names repeat across calls (the SIP
# dispatch rule templates them on the DID, ``cs-{call.to}``), so a permanent
# latch would poison every later legitimate overflow on the same DID. Safe
# here because the chain always terminates in transferred-or-deleted — there
# is no failure-retry loop to guard against. Naturally bounded by the number
# of concurrent overflows.
_overflow_in_progress: set[str] = set()


def max_concurrent_calls() -> int:
    """The LIVEKIT admission limit; 0 disables the gate."""
    raw = (os.environ.get("LIVEKIT_MAX_CONCURRENT_CALLS") or "").strip()
    return int(raw) if raw else DEFAULT_MAX_CONCURRENT_CALLS


def max_inflight_overflow() -> int:
    raw = (os.environ.get("CAPACITY_OVERFLOW_MAX_INFLIGHT") or "").strip()
    return int(raw) if raw else DEFAULT_MAX_INFLIGHT_OVERFLOW


def overflow_transfer_to() -> str | None:
    """Effective overflow REFER target, or None when nothing is configured.

    ``CAPACITY_OVERFLOW_TRANSFER_TO`` when set, else the safetynet fallback
    queue — the two nearly always point at the same human queue, and reusing
    it avoids a second env var that silently drifts.
    """
    value = (os.environ.get("CAPACITY_OVERFLOW_TRANSFER_TO") or "").strip()
    if value:
        return value
    from api.services.pipecat.livekit_safetynet import fallback_queue

    return fallback_queue()


def _premium_rate(destination: str) -> bool:
    number = destination.strip()
    if number.startswith("tel:"):
        number = number[len("tel:") :]
    elif number.startswith("sip:"):
        number = number[len("sip:") :].split("@", 1)[0]
    return number.lstrip("+").startswith(PREMIUM_RATE_PREFIXES)


def validate_capacity_config() -> None:
    """Fail loudly at startup on malformed capacity config (security F8).

    Mirrors ``validate_safetynet_config``: the overflow path has no engine and
    no call-time validation hook, so a bad limit or target must stop boot.
    """
    raw_limit = (os.environ.get("LIVEKIT_MAX_CONCURRENT_CALLS") or "").strip()
    if raw_limit:
        try:
            limit = int(raw_limit)
        except ValueError as e:
            raise RuntimeError(
                f"LIVEKIT_MAX_CONCURRENT_CALLS is not an integer: {raw_limit!r}"
            ) from e
        if limit < 0:
            raise RuntimeError(
                f"LIVEKIT_MAX_CONCURRENT_CALLS must be >= 0, got {limit}"
            )
        if limit == 0:
            logger.info(
                "LIVEKIT_MAX_CONCURRENT_CALLS=0; capacity gate disabled "
                "(dispatch behaves exactly as before the gate)"
            )

    raw_inflight = (os.environ.get("CAPACITY_OVERFLOW_MAX_INFLIGHT") or "").strip()
    if raw_inflight:
        try:
            inflight = int(raw_inflight)
        except ValueError as e:
            raise RuntimeError(
                f"CAPACITY_OVERFLOW_MAX_INFLIGHT is not an integer: {raw_inflight!r}"
            ) from e
        if inflight <= 0:
            raise RuntimeError(
                f"CAPACITY_OVERFLOW_MAX_INFLIGHT must be > 0, got {inflight}"
            )

    explicit = (os.environ.get("CAPACITY_OVERFLOW_TRANSFER_TO") or "").strip()
    if explicit and not valid_destination(explicit):
        raise RuntimeError(
            f"CAPACITY_OVERFLOW_TRANSFER_TO {explicit!r} is not tel:+E164 or "
            "sip:user@host"
        )
    # Guard the *effective* target: the safetynet fallback carries the same
    # auto-REFER blast radius when it is what overflow will actually dial.
    effective = overflow_transfer_to()
    if effective is not None and _premium_rate(effective):
        raise RuntimeError(
            f"capacity overflow target {effective!r} matches a premium-rate "
            f"prefix {PREMIUM_RATE_PREFIXES}; refusing to boot"
        )
    if effective is None:
        logger.warning(
            "Neither CAPACITY_OVERFLOW_TRANSFER_TO nor SAFETYNET_FALLBACK_QUEUE "
            "is set; full-capacity calls will be ended explicitly (room delete)"
        )


async def _gate_allows(workflow_id: int, user_id: int, now: datetime) -> bool:
    """營運中 ∧ 隊列健康 — the same composed verdict ``execute_cold_transfer``
    uses (``resolve_transfer_decision``, single truth, security F7).

    Inputs come from the workflow's ``transfer_call`` tool config (shared
    lookup). A failed lookup degrades to "unconfigured" — open hours, health
    unchecked — matching the in-call gate's unset semantics; the warning below
    is the ops signal that the health dimension was skipped, not passed.
    """
    from api.db import db_client
    from api.services.pipecat.transfer_call_config import find_transfer_call_config

    config: dict | None = None
    try:
        workflow = await db_client.get_workflow(workflow_id, user_id)
        if workflow is not None and workflow.organization_id:
            config = await find_transfer_call_config(workflow, workflow.organization_id)
    except Exception as e:
        logger.warning(
            f"capacity gate config lookup failed ({e}); "
            "degrading to unconfigured — hours open, queue health unchecked"
        )
    config = config or {}

    decision = await resolve_transfer_decision(
        config.get("schedule"), None, now, config
    )
    return decision is TransferDecision.REFER


async def capacity_overflow(
    room_name: str,
    *,
    active: int,
    limit: int,
    workflow_id: int,
    user_id: int,
    lk=None,
    now: datetime | None = None,
) -> None:
    """Background action chain for a capacity-rejected call. Never raises.

    guard → flood valve → target → 營運中 ∧ 隊列健康 gate → wait for the SIP
    caller → REFER; any non-viable step deletes the room so the caller hears a
    hangup (C4). Emits exactly one ``capacity.rejected`` per rejection with
    the final outcome; a redelivered ``room_started`` blocked by the guard
    emits nothing.
    """
    if room_name in _overflow_in_progress:
        logger.info(
            f"capacity overflow already in flight for {room_name}; "
            "skipping redelivered trigger"
        )
        return
    flood = len(_overflow_in_progress) >= max_inflight_overflow()
    _overflow_in_progress.add(room_name)

    outcome = "terminated"
    reason = "unknown"

    async def _act() -> tuple[str, str]:
        from api.services.pipecat.livekit_cold_transfer import (
            cold_transfer_to_human,
            livekit_api,
            wait_for_sip_participant,
        )
        from api.services.pipecat.livekit_safetynet import delete_room

        async with livekit_api(lk) as client:
            if flood:
                await delete_room(room_name, client)
                return "terminated", "overflow_flood"
            destination = overflow_transfer_to()
            if destination is None:
                await delete_room(room_name, client)
                return "terminated", "no_target"
            if not await _gate_allows(
                workflow_id, user_id, now or datetime.now(timezone.utc)
            ):
                await delete_room(room_name, client)
                return "terminated", "gate_closed"
            identity = await wait_for_sip_participant(room_name, lk=client)
            if identity is None:
                await delete_room(room_name, client)
                return "terminated", "no_sip_caller"
            result = await cold_transfer_to_human(
                room_name,
                destination,
                lk=client,
                participant_identity=identity,
            )
            if result.get("status") == "success":
                return "transferred", "capacity"
            await delete_room(room_name, client)
            return "terminated", result.get("reason", "refer_failed")

    try:
        # A hung LiveKit call must not pin this room's guard entry and its
        # flood-valve slot forever — bound the whole chain, then take the
        # recovery-delete leg like any other failure.
        outcome, reason = await asyncio.wait_for(
            _act(), timeout=OVERFLOW_ACTION_TIMEOUT_SECONDS
        )
    except Exception as e:
        logger.exception(f"capacity overflow failed for {room_name}: {e}")
        reason = (
            "overflow_timeout"
            if isinstance(e, asyncio.TimeoutError)
            else "overflow_error"
        )
        try:
            from api.services.pipecat.livekit_cold_transfer import livekit_api
            from api.services.pipecat.livekit_safetynet import delete_room

            async with livekit_api(lk) as client:
                await delete_room(room_name, client)
        except Exception:
            logger.warning(f"capacity overflow could not delete room {room_name}")
    finally:
        _overflow_in_progress.discard(room_name)
        call_events.emit(
            "capacity.rejected",
            room_name=room_name,
            reason=reason,
            active=active,
            limit=limit,
            outcome=outcome,
        )
