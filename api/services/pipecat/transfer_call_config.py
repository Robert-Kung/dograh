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


def revalidate_transfer_config(config: dict) -> dict | None:
    """Re-check the shapes this config carries, at read time (issue #3).

    **Public because this lookup is not the only reader.** The AI-initiated
    transfer tool handler (``pipecat_engine_custom_tools`` /
    ``transfer_call_handler``) reads ``tool.definition["config"]`` straight off
    the ORM row — it wants *that* tool's config, not "the workflow's first
    transfer_call tool", so it cannot go through
    :func:`find_transfer_call_config` without changing behaviour on a workflow
    carrying two transfer tools. It calls this directly instead. Before W2a's
    security review found it (M-8), that path — the highest-volume trigger,
    the caller simply asking for a human — was the one reader with no
    re-validation at all.

    The write path validates these fields, but nothing re-checks them on the
    way *out*: a ``PUT /tools`` takes effect on the next call with no role
    check, and the value may also predate the current rules or have been
    written by a path that bypassed them entirely. Reading without re-checking
    makes the write-time validator the only gate, and it is not a gate that
    covers the database's existing contents.

    Field by field, because the blast radius differs:

    - **``destination`` bad → the destination is blanked, the config survives.**

      It used to return ``None``, and that was wrong in two ways at once
      (2026-08-19 review B-1 / M-5). ``None`` is indistinguishable from "this
      workflow has no transfer_call tool", and callers branch on exactly that:

        * ``resolve_press0_gate`` guards its alert on
          ``if transfer_config and not valid_destination(...)`` — with ``None``
          it fell through to a bare ``logger.info``, so a misconfigured
          deployment lost both the ``transfer.failed`` **alert dispatch** and the
          ``record_call_outcome`` annotation, and read as clean AI completions
          in the queryable layer. That alert branch exists *precisely* for this
          case; W0 added it.
        * ``capacity_gate._gate_allows`` does ``config = config or {}`` — with
          ``None``, an empty dict means "no schedule" (= always open) and
          ``queue_is_healthy({})`` returns True. One bad destination silently
          switched off **both** the business-hours gate and the queue-health gate.

      Blanking keeps the config truthy, so every existing "configured but
      malformed" path fires as designed, and ``valid_destination("")`` is False
      so nothing gets dialled.
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
        log_artifact_missing("revalidate_transfer_config", exc)
        logger.bind(call_event="transfer.config_unvalidatable").error(
            "transfer.config_unvalidatable: shared REFER URI parser unavailable; "
            "blanking the destination (fail-closed, W2a)"
        )
        return dict(config, destination="")

    if not parsed.ok:
        logger.bind(call_event="transfer.config_rejected").error(
            f"transfer.config_rejected field=destination: {parsed.reason}; "
            f"destination blanked — the configured-but-malformed path takes over "
            f"(W2a issue #3)"
        )
        return dict(config, destination="")

    # Premium-rate guard (2026-08-19 review M-1). The write path runs shape
    # **and** premium-rate; the read path ran only shape — so a `tel:+1900…`
    # sitting in the database was shape-perfect and dialled every time. The
    # read path exists precisely because the database's contents never went
    # through the write path.
    from api.services.pipecat.capacity_gate import PREMIUM_RATE_PREFIXES, _premium_rate

    if _premium_rate(destination):
        logger.bind(call_event="transfer.config_rejected").error(
            f"transfer.config_rejected field=destination: matches a premium-rate "
            f"prefix {PREMIUM_RATE_PREFIXES}; destination blanked (review M-1)"
        )
        return dict(config, destination="")

    checked = dict(config)

    alternate = checked.get("alternateDestination")
    if alternate is not None and str(alternate).strip():
        alt_parsed = parse_refer_uri(alternate)
        if alt_parsed.ok and _premium_rate(alternate):
            logger.bind(call_event="transfer.config_rejected").error(
                "transfer.config_rejected field=alternateDestination: premium-rate "
                "prefix; after-hours alternate branch disabled (review M-1)"
            )
            checked.pop("alternateDestination", None)
        elif not alt_parsed.ok:
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
    ``config`` — **re-validated**, see :func:`revalidate_transfer_config`.

    This is a ``get_tools_by_uuids`` path that deliberately does **not** consult
    the enabled set, unlike the three in ``pipecat_engine``/
    ``pipecat_engine_custom_tools`` (review B-3). Those three decide what the
    LLM may call; this one feeds press-0 and capacity overflow, which are
    platform safety nets the caller reaches without the LLM. Gating it on the
    canon would mean an unreadable bind mount silently removes the route to a
    human — fail-closed in the wrong direction for C4. What it does instead is
    re-validate the value, which is the check that actually matters here.
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
            return revalidate_transfer_config(
                (tool.definition or {}).get("config", {}) or {}
            )
    return None
