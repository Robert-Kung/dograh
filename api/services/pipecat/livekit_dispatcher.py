"""LiveKit inbound dispatcher (S-L1-DISPATCH).

Resolves a LiveKit SIP inbound call into a Dograh agent run: take the dialed
DID from the SIP caller's attributes, resolve the workflow, create a LIVEKIT
workflow_run, sign an agent token, and launch ``run_pipeline_livekit``
non-blockingly.

Trigger (livekit-event-wiring 設計 C): the ``participant_joined`` webhook of
a SIP participant in a ``cs-`` room — not ``room_started``. The dispatch rule
names rooms ``cs-_<dialed>_<random>`` (callee rule, randomized), so the room
name is never a DID source, and the SIP attributes are in the event itself
(``sip.trunkPhoneNumber``, ``sip.callID``; task 0.3). One call is dispatched
once, keyed by ``sip.callID`` with a two-phase claim/commit/release
(:class:`DispatchDedup`). Every path out of a claimed dispatch is either a
launched run or a hand-off to ``fallback``/overflow (C4); nothing raises to
the route.
"""

import asyncio
import os
import re
import time
from typing import Awaitable, Callable, Optional

from loguru import logger

from api.utils.telephony_address import normalize_telephony_address

DEFAULT_ROOM_PREFIX = "cs-"

# Carriers may send the dialed number in national format ("0212345678"; task
# 0.3 measured the raw attribute). The DID table is registered in E.164.
DID_COUNTRY_HINT = "TW"

SIP_DIALED_ATTRIBUTE = "sip.trunkPhoneNumber"
SIP_CALL_ID_ATTRIBUTE = "sip.callID"

# Must exceed the webhook JWT lifetime (exp = issue + 300 s, task 0.3) so a
# redelivery LiveKit still signs as valid always meets the committed entry.
DISPATCH_DEDUP_TTL_SECONDS = 30 * 60

# Transient resolver failures (DB blip) heal in-process: the webhook is acked
# at once and never 5xx'd, so a LiveKit redelivery is not the retry path.
RESOLVER_RETRY_DELAYS_SECONDS = (0.5, 1.0)

# Upper bound on one dispatch attempt; past it the call goes to the fallback.
DISPATCH_TIMEOUT_SECONDS = 15.0

# DID -> (workflow_id, user_id). Storage/owner is an open question
# (relates to S-L6-ROUTING); injected so this layer makes no assumption.
DidResolver = Callable[[str], Awaitable[Optional[tuple[int, int]]]]
Fallback = Callable[[str, str, Optional[int]], Awaitable[None]]


def did_from_sip_attributes(attributes: dict) -> str | None:
    """E.164 DID from the SIP caller's ``sip.trunkPhoneNumber`` (C6), or None."""
    raw = ((attributes or {}).get(SIP_DIALED_ATTRIBUTE) or "").strip()
    # No hint for an explicitly international number: the normalizer strips
    # "+" before comparing dial codes, so hinting "+1555…" or "001555…" would
    # prefix 886 to a foreign number (review D-02). Everything else — "02…",
    # "2…" without the trunk prefix, "886…" without "+" — keeps the hint
    # (re-review F13).
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+"):
        hint = None
    elif digits.startswith("00"):
        raw, hint = "+" + digits[2:], None
    else:
        hint = DID_COUNTRY_HINT
    try:
        normalized = normalize_telephony_address(raw, country_hint=hint)
    except ValueError:
        return None
    if normalized.address_type != "pstn":
        return None
    return normalized.canonical or None


def dispatch_key(participant) -> str:
    """Per-call dedup key: ``sip.callID``, else the participant sid (M12)."""
    attributes = dict(getattr(participant, "attributes", {}) or {})
    return attributes.get(SIP_CALL_ID_ATTRIBUTE) or str(participant.sid)


class DispatchDedup:
    """Two-phase per-call dedup (review H2), after queue's webhook_dedup.

    ``claim`` (no await between check and insert) marks a call in flight;
    ``commit`` once a run was launched or the call was handed to the
    fallback/overflow; ``release`` when neither happened. Only committed
    entries are replay evidence — an unfinished claim never is, so a dispatch
    that died before handing the call off cannot swallow the next attempt.
    In-process by design: webhooks reach a single worker (:8000).
    """

    def __init__(
        self, ttl_seconds: float = DISPATCH_DEDUP_TTL_SECONDS, clock=time.monotonic
    ):
        self._ttl = ttl_seconds
        self._clock = clock
        self._in_flight: dict[str, float] = {}
        self._committed: dict[str, float] = {}

    def _purge(self) -> None:
        cutoff = self._clock() - self._ttl
        for table in (self._in_flight, self._committed):
            for key in [k for k, t in table.items() if t < cutoff]:
                del table[key]

    def claim(self, key: str) -> bool:
        self._purge()
        if key in self._in_flight or key in self._committed:
            return False
        self._in_flight[key] = self._clock()
        return True

    def commit(self, key: str) -> None:
        self._in_flight.pop(key, None)
        self._committed[key] = self._clock()

    def release(self, key: str) -> None:
        self._in_flight.pop(key, None)

    def is_known(self, key: str) -> bool:
        self._purge()
        return key in self._in_flight or key in self._committed


dispatch_dedup = DispatchDedup()


async def _bounded(coro):
    return await asyncio.wait_for(coro, timeout=DISPATCH_TIMEOUT_SECONDS)


async def _resolve_with_retry(resolver: DidResolver, did: str):
    for delay in (*RESOLVER_RETRY_DELAYS_SECONDS, None):
        try:
            return await resolver(did)
        except Exception as e:
            if delay is None:
                raise
            logger.warning(f"DID resolver failed ({type(e).__name__}); retrying")
            await asyncio.sleep(delay)


def _sign_agent_token(room_name: str, identity: str) -> str:
    from livekit import api

    return (
        api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
        .with_identity(identity)
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
            )
        )
        .to_jwt()
    )


async def dispatch_livekit_call(
    room_name: str,
    sip_attributes: dict,
    resolver: DidResolver,
    fallback: Fallback,
    livekit_url: str | None = None,
    *,
    dedup_key: str | None = None,
    dedup: DispatchDedup | None = None,
) -> None:
    """Resolve an inbound LiveKit call and launch the agent (non-blocking).

    Never fails silently (C4): no/unmapped DID, a launch error, or **any**
    exception on the way routes to ``fallback(room_name, reason, run_id)``
    (review H2 — the resolver used to sit outside the try and 500 the
    webhook). The pipeline itself contains its fatal errors via the safetynet
    and re-raises; the task's done callback routes the escaped exception back
    through ``fallback`` as a last resort, deduped by the safetynet's run-id
    latch (S-L3-SAFETYNET). With ``dedup_key`` the caller's claim is committed
    once the call was handed off, released otherwise.
    """
    dedup = dedup or dispatch_dedup
    handed_off = False
    try:
        handed_off = await _dispatch(
            room_name, sip_attributes, resolver, fallback, livekit_url
        )
    except Exception as e:
        logger.exception(f"LiveKit dispatch failed for {room_name}: {e}")
        # Before a run exists: the launch section handles its own failures.
        await fallback(room_name, "dispatch_error", None)
        handed_off = True
    finally:
        if dedup_key is not None:
            if handed_off:
                dedup.commit(dedup_key)
            else:
                dedup.release(dedup_key)


async def _dispatch(
    room_name: str,
    sip_attributes: dict,
    resolver: DidResolver,
    fallback: Fallback,
    livekit_url: str | None,
) -> bool:
    """One dispatch attempt; True once the call is launched or handed off."""
    did = did_from_sip_attributes(sip_attributes)
    if not did:
        await fallback(room_name, "no_did", None)
        return True

    # Bounded (review D-05): a DB await that hangs without raising would hold
    # the claim — and the reconciler skips claimed calls — leaving the caller
    # in silence (C4). Only the lookups are bounded, never a fallback: a
    # fallback cut short mid-REFER would be retried as "dispatch_error" and
    # fire twice (re-review F5).
    resolved = await asyncio.wait_for(
        _resolve_with_retry(resolver, did), timeout=DISPATCH_TIMEOUT_SECONDS
    )
    if not resolved:
        await fallback(room_name, "unmapped_did", None)
        return True

    from api.db import db_client
    from api.enums import CallType, WorkflowRunMode
    from api.services.pipecat.active_calls import (
        livekit_active_call_count,
        release_slot,
        reserved_slot_count,
        try_acquire_slot,
    )
    from api.services.pipecat.capacity_gate import (
        capacity_overflow,
        max_concurrent_calls,
    )
    from api.services.pipecat.livekit_safetynet import spawn
    from api.services.pipecat.run_pipeline import run_pipeline_livekit

    workflow_id, user_id = resolved

    # S-L9-SCALE admission — before any run/token/pipeline resource exists.
    # The decision is synchronous (check-and-reserve, no await gap); the
    # overflow action chain runs in the background so the webhook acks fast.
    limit = max_concurrent_calls()
    gate_enabled = limit > 0
    if gate_enabled and not try_acquire_slot(limit):
        spawn(
            capacity_overflow(
                room_name,
                active=livekit_active_call_count() + reserved_slot_count(),
                limit=limit,
                workflow_id=workflow_id,
                user_id=user_id,
            )
        )
        return True

    workflow_run_id: Optional[int] = None
    slot_reserved = gate_enabled
    try:
        workflow_run = await _bounded(
            db_client.create_workflow_run(
                name=f"livekit-{room_name}",
                workflow_id=workflow_id,
                mode=WorkflowRunMode.LIVEKIT.value,
                user_id=user_id,
                call_type=CallType.INBOUND,
                initial_context={
                    "did": did,
                    "room_name": room_name,
                    "direction": "inbound",
                },
            )
        )
        workflow_run_id = workflow_run.id

        url = livekit_url or os.environ["LIVEKIT_URL"]
        token = _sign_agent_token(room_name, f"agent-{workflow_run_id}")

        def _on_pipeline_done(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is None:
                return
            logger.opt(exception=exc).error(
                f"LiveKit pipeline task died for {room_name}: {exc}"
            )
            spawn(fallback(room_name, "launch_failed", workflow_run_id))

        task = asyncio.create_task(
            run_pipeline_livekit(
                url=url,
                token=token,
                room_name=room_name,
                workflow_id=workflow_id,
                workflow_run_id=workflow_run_id,
                user_id=user_id,
                reserved=gate_enabled,
            )
        )
        task.add_done_callback(_on_pipeline_done)
        # The pipeline task now owns the slot: its first line converts the
        # reservation to an active call, and its finally releases it.
        slot_reserved = False
    except Exception as e:
        logger.exception(f"LiveKit pipeline launch failed for {room_name}: {e}")
        await fallback(room_name, "launch_failed", workflow_run_id)
    finally:
        if slot_reserved:
            release_slot()
    return True


WEBHOOK_REJECTED_EVENT = "livekit.webhook_rejected"
WEBHOOK_REJECTED_WINDOW_SECONDS = 60.0

_rejected = {"last_emit": None, "suppressed": 0}


def record_webhook_rejected(error: Exception, clock=time.monotonic) -> None:
    """Signature failure on the dograh webhook path (security L4).

    The path secret was right, so this is a key mismatch or a forgery attempt
    from inside the deployment; either way calls are not being dispatched.
    One event per window, carrying how many were folded into it.
    """
    from api.services.observability.call_events import emit

    t = clock()
    last = _rejected["last_emit"]
    if last is not None and t - last < WEBHOOK_REJECTED_WINDOW_SECONDS:
        _rejected["suppressed"] += 1
        return
    suppressed = _rejected["suppressed"]
    _rejected.update(last_emit=t, suppressed=0)
    emit(
        WEBHOOK_REJECTED_EVENT,
        room_name="",
        reason=type(error).__name__,
        suppressed=suppressed,
    )


def handle_webhook_event(event, resolver: DidResolver, fallback: Fallback) -> bool:
    """Route one verified webhook event; True when a dispatch was started.

    Only a SIP participant joining a ``cs-`` room dispatches (設計 C). Every
    other event — ``room_started``, the agent's own join, any non-``cs-`` room
    — is acknowledged and dropped without touching the participant's
    attributes: this service receives every room's events, including the
    human-queue rooms (security L5, data minimization). Synchronous: the claim
    happens before the first await, and the dispatch runs in the background
    so the webhook acks at once.
    """
    from livekit.protocol.models import ParticipantInfo

    from api.services.pipecat.livekit_safetynet import spawn

    if event.event != "participant_joined":
        return False
    room_name = event.room.name if event.room else ""
    if not room_name.startswith(DEFAULT_ROOM_PREFIX):
        return False
    participant = event.participant
    if participant is None or participant.kind != ParticipantInfo.Kind.SIP:
        return False

    key = dispatch_key(participant)
    if not dispatch_dedup.claim(key):
        logger.info(f"LiveKit dispatch already claimed for {room_name}; ignoring")
        return False
    try:
        spawn(
            dispatch_livekit_call(
                room_name,
                dict(participant.attributes),
                resolver,
                fallback,
                dedup_key=key,
            )
        )
    except Exception:
        dispatch_dedup.release(key)
        raise
    return True
