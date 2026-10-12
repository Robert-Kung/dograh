"""Smoke-call recognition (local-deploy-sip-smoke design D2 r2/D8).

A smoke call is recognised by its **room name**: only the smoke dispatch rule
-- created server side by whoever holds the LiveKit key, inside the smoke
window -- names rooms with ``SMOKE_ROOM_PREFIX``. The caller cannot choose the
room name, and production callee rules name rooms ``cs-_<number>_…``, which can
never start with the smoke prefix.

D2 r1 read the rule's ``ccp.smoke`` attribute from the ``participant_joined``
webhook, but LiveKit does not guarantee rule or header-mapped attributes are in
that event (task 0.3 diagnosis, 2026-10-12): the attribute was on the
participant yet missing from the webhook. The room name is in every event,
from the first one, so no lookup and no Redis registry is needed.
"""

SMOKE_ROOM_PREFIX = "cs-zzsmoke-"
# The run's tag. Written by the dispatcher on every LiveKit run (true or false)
# and stripped from every other source that merges into initial_context --
# workflow template variables, pre-call fetch results -- so neither an editor
# nor a CRM response can make a production call vanish from the reports.
RUN_CONTEXT_KEY = "ccp_smoke"
RESERVED_CONTEXT_KEYS = frozenset({RUN_CONTEXT_KEY})


def is_smoke_room(room_name: str | None) -> bool:
    return bool(room_name) and room_name.startswith(SMOKE_ROOM_PREFIX)


def without_reserved(context: dict | None) -> dict:
    """A merge source minus the dispatcher-owned keys."""
    return {k: v for k, v in (context or {}).items() if k not in RESERVED_CONTEXT_KEYS}
