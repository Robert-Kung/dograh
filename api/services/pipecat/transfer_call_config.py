"""Workflow-level ``transfer_call`` tool config lookup (shared).

The transfer gate's inputs — business-hours schedule and queue-health keys —
live in the workflow's ``transfer_call`` tool config. Two consumers need it:
the in-call engine (voice tool / press-0, via
``PipecatEngine.resolve_transfer_call_config``) and the engine-less capacity
overflow chain (S-L9-SCALE), which has a resolved workflow but no run. One
lookup so the two can never disagree on where the gate config comes from.
"""

from urllib.parse import urlsplit

from loguru import logger

from api.db import db_client
from api.enums import ToolCategory

# Health-probe URLs the gate is allowed to call. The canonical, richer rule
# (allowlisted hosts, no userinfo, no IDN, explicit ports) is
# ``feature_scope_check._check_url`` in the platform repo — it runs at
# deployment time and is **not** mounted into this container. What is checked
# here is the subset that matters at call time: is this a thing we are willing
# to make an outbound request to at all.
_HEALTH_URL_SCHEMES = ("http", "https")


def _health_url_problem(value: str) -> str | None:
    """Minimal call-time shape check for ``queueHealthUrl``. None == usable."""
    if any(ch.isspace() for ch in value):
        return "contains whitespace"
    parts = urlsplit(value)
    if parts.scheme not in _HEALTH_URL_SCHEMES:
        return "scheme is not http/https"
    if "@" in parts.netloc:
        return "carries userinfo"
    if not parts.hostname:
        return "has no host"
    return None


def _revalidate(config: dict) -> dict | None:
    """Re-check the shapes this config carries, at read time (issue #3).

    The write path validates these fields, but nothing re-checks them on the
    way *out*: a ``PUT /tools`` takes effect on the next call with no role
    check, and the value may also predate the current rules or have been
    written by a path that bypassed them entirely. Reading without re-checking
    makes the write-time validator the only gate, and it is not a gate that
    covers the database's existing contents.

    Field by field, because the blast radius differs:

    - **``destination`` bad → the whole config is unusable** (return None).
      Callers then take their existing no-transfer-config path, which since W0
      emits ``transfer.failed`` rather than silently not installing the gate.
      Dialling an unvalidated destination is the outcome this must not have:
      it is the vishing/call-interception surface (R-E).
    - **``alternateDestination`` bad → drop that key only.** It is the
      after-hours branch; killing the main transfer path over it would trade a
      degraded branch for a dead one.
    - **``queueHealthUrl`` bad → drop the health keys only.** The transfer then
      proceeds without a health probe, which is the documented pre-S-L5-QUEUE
      behaviour.

    Every drop is logged at high signal. Nothing here is silent — that is the
    whole point of the task (MUST NOT silently use).
    """
    from api.services.platform_scope import (
        PlatformArtifactMissing,
        log_artifact_missing,
        parse_refer_uri,
    )

    destination = config.get("destination")
    try:
        parsed = parse_refer_uri(destination)
    except PlatformArtifactMissing as exc:
        # Fail closed, matching the call-time tool filter: with the parser gone
        # we cannot tell a queue from an attacker's SIP host, and the two
        # artifacts are mounted together, so the tool itself is about to be
        # dropped by the enabled-set filter anyway.
        log_artifact_missing("find_transfer_call_config", exc)
        logger.bind(call_event="transfer.config_unvalidatable").error(
            "transfer.config_unvalidatable: shared REFER URI parser unavailable; "
            "treating the transfer_call config as absent (fail-closed, W2a)"
        )
        return None

    if not parsed.ok:
        logger.bind(call_event="transfer.config_rejected").error(
            f"transfer.config_rejected field=destination: {parsed.reason}; "
            f"transfer_call config treated as absent (W2a issue #3)"
        )
        return None

    checked = dict(config)

    alternate = checked.get("alternateDestination")
    if alternate is not None and str(alternate).strip():
        alt_parsed = parse_refer_uri(alternate)
        if not alt_parsed.ok:
            logger.bind(call_event="transfer.config_rejected").error(
                f"transfer.config_rejected field=alternateDestination: "
                f"{alt_parsed.reason}; after-hours alternate branch disabled for "
                f"this call (W2a issue #3)"
            )
            checked.pop("alternateDestination", None)

    health_url = checked.get("queueHealthUrl")
    if health_url is not None and str(health_url).strip():
        problem = _health_url_problem(str(health_url))
        if problem:
            logger.bind(call_event="transfer.config_rejected").error(
                f"transfer.config_rejected field=queueHealthUrl: {problem}; "
                f"queue health probe disabled for this call (W2a issue #3)"
            )
            for key in ("queueHealthUrl", "queueHealthToken"):
                checked.pop(key, None)

    return checked


async def find_transfer_call_config(workflow, organization_id: int) -> dict | None:
    """Return the workflow's ``transfer_call`` tool config, or None if absent.

    Scans every node's tools (a press-0 safety net is global, so the target is
    workflow-wide, not per-node) and returns the first ``transfer_call`` tool's
    ``config`` — **re-validated**, see :func:`_revalidate`.
    """
    tool_uuids: set[str] = set()
    for node in workflow.nodes.values():
        for tu in getattr(node, "tool_uuids", None) or []:
            tool_uuids.add(tu)
    if not tool_uuids:
        return None

    tools = await db_client.get_tools_by_uuids(list(tool_uuids), organization_id)
    for tool in tools:
        if tool.category == ToolCategory.TRANSFER_CALL.value:
            return _revalidate((tool.definition or {}).get("config", {}) or {})
    return None
