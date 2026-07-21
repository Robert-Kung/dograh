"""In-process registry of active pipeline runs (live voice calls).

Each uvicorn worker tracks the calls it is currently running so a deploy
orchestrator can *drain* the worker before stopping it: poll the count, wait for
zero, then send SIGTERM. Sending SIGTERM while calls are live makes uvicorn
force-close their WebSockets (close code 1012), which cuts the calls instead of
letting them finish — so the wait has to happen first.

The registry is deliberately per-process. That is exactly the unit that gets
drained: one uvicorn process per VM port (see ``scripts/rolling_update.sh``) or
one uvicorn process per Kubernetes pod (drained via a ``preStop`` hook). The
count is exposed read-only at ``GET /api/v1/health/active-calls`` and is also a
natural autoscaling signal (concurrent calls per worker).

Access is single-threaded (asyncio event loop), so no lock is needed. A set of
run ids — rather than a bare counter — keeps register/unregister idempotent and
makes the in-flight runs inspectable for debugging.

S-L9-SCALE adds the capacity-gate admission primitives on top. The gate counts
LIVEKIT inbound AI calls only — the cross-transport set above keeps its drain
contract untouched — so admission uses a separate LIVEKIT-scoped set plus a
reservation counter. ``try_acquire_slot`` is deliberately synchronous: the
dispatch path has ``await`` points between check and register, and a
check-then-register split across them would over-admit concurrent webhooks.
"""

_active_run_ids: set[int] = set()

_livekit_run_ids: set[int] = set()
_reserved_slots: int = 0


def try_acquire_slot(limit: int) -> bool:
    """Reserve one LIVEKIT admission slot; False when at/over ``limit``.

    Synchronous check-and-reserve — no await gap, so it is atomic on the
    event loop. The reservation is converted to an active call by
    ``register_active_call(..., reserved=True)`` or handed back via
    ``release_slot`` on any dispatch failure before the pipeline starts.
    """
    global _reserved_slots
    if len(_livekit_run_ids) + _reserved_slots >= limit:
        return False
    _reserved_slots += 1
    return True


def release_slot() -> None:
    """Return an unconverted reservation. Never goes negative (fail-safe)."""
    global _reserved_slots
    if _reserved_slots > 0:
        _reserved_slots -= 1


def register_active_call(workflow_run_id: int, *, reserved: bool = False) -> None:
    """Mark a pipeline run as active in this worker.

    ``reserved=True`` is passed only by the LIVEKIT dispatch path after a
    successful ``try_acquire_slot``: the reservation converts into the
    LIVEKIT-scoped active count. Every other transport (and the gate-disabled
    LIVEKIT path) never acquired, so the default must stay False — converting
    a reservation that does not exist would underflow the gate arithmetic.
    """
    _active_run_ids.add(workflow_run_id)
    if reserved:
        _livekit_run_ids.add(workflow_run_id)
        release_slot()


def unregister_active_call(workflow_run_id: int) -> None:
    """Mark a pipeline run as finished in this worker."""
    _active_run_ids.discard(workflow_run_id)
    _livekit_run_ids.discard(workflow_run_id)


def active_call_count() -> int:
    """Number of pipeline runs currently active in this worker."""
    return len(_active_run_ids)


def livekit_active_call_count() -> int:
    """LIVEKIT-scoped active runs — the admission side of the capacity gate."""
    return len(_livekit_run_ids)


def reserved_slot_count() -> int:
    """Admission slots reserved but not yet converted to active runs."""
    return _reserved_slots
