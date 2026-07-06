"""Transfer handoff summary job (S-L4-SCREENPOP, D7).

Runs in the ARQ worker process — it survives the engine teardown and
deployment restarts that make in-process completion hooks impossible
(the pipeline's MCP sessions are already closed by the time the run
winds down). Input is the snapshot dict captured synchronously at REFER
time; ticket writes go through the same MCP contract as the skeleton.

Failure semantics: at most one retry per write, the skeleton ticket is
never touched on failure, and every failure leaves a diagnostic log. A
failed REFER gets a `transfer_failed` note instead of a summary — no
ghost "transferred" tickets, and no LLM spend on a call that stayed
with the AI.
"""

from loguru import logger

from api.db import db_client
from api.services.tickets import contract
from api.services.tickets.config import resolve_ticket_server_config


async def _append_note_with_retry(config, ticket_id: str, note_type: str, content):
    """One retry max; returns True when the note landed."""
    from api.services.pipecat.transfer_context_handoff import call_ticket_tool

    last_error = None
    for attempt in (1, 2):
        try:
            result = await call_ticket_tool(
                config,
                "append_ticket_note",
                {"ticket_id": ticket_id, "note_type": note_type, "content": content},
            )
            if not contract.is_error(result):
                return True
            last_error = result["error"]
            if not last_error.get("retryable"):
                break
        except Exception as e:
            last_error = repr(e)
    logger.warning(
        f"context_write: failed (stage=job:{note_type}) — "
        f"ticket {ticket_id}: {last_error}"
    )
    return False


async def _ensure_ticket_exists(config, snapshot: dict) -> None:
    """Idempotent get-or-create before any append.

    The skeleton write is a detached background task on the call's event loop;
    this job can outrun it (append would hit NOT_FOUND, which is
    non-retryable) and a worker drain/restart can drop it entirely. Creating
    here is a no-op when the skeleton already landed — the existing ticket
    wins, including its caller_number. Failures are logged and deliberately
    not fatal: the append that follows produces the definitive error."""
    from api.services.pipecat.transfer_context_handoff import call_ticket_tool

    try:
        result = await call_ticket_tool(
            config,
            "create_ticket",
            {
                "ticket_id": snapshot["ticket_id"],
                "workflow_run_id": snapshot["workflow_run_id"],
                "caller_number": snapshot.get("caller_number", ""),
                "room_name": snapshot.get("room_name", ""),
                "transfer_reason": snapshot.get("transfer_reason", ""),
            },
        )
        if contract.is_error(result):
            logger.warning(
                f"job-side ticket get-or-create rejected for "
                f"{snapshot['ticket_id']}: {result['error']}"
            )
    except Exception as e:
        logger.warning(
            f"job-side ticket get-or-create failed for {snapshot['ticket_id']}: {e!r}"
        )


async def summarize_transfer_handoff(_ctx, snapshot: dict) -> None:
    ticket_id = snapshot.get("ticket_id")
    workflow_run_id = snapshot.get("workflow_run_id")
    organization_id = snapshot.get("organization_id")
    if not ticket_id or not workflow_run_id or not organization_id:
        logger.warning(f"transfer handoff job got malformed snapshot: {snapshot!r}")
        return

    # get_workflow_run selectinloads .definition (get_workflow_run_by_id
    # doesn't, and lazy-loading outside the session raises in async SQLA).
    workflow_run = await db_client.get_workflow_run(workflow_run_id)
    run_configs = (
        (workflow_run.definition.workflow_configurations or {})
        if workflow_run and workflow_run.definition
        else {}
    )
    config = await resolve_ticket_server_config(organization_id, run_configs)
    if config is None:
        logger.warning(
            f"ticket server config vanished before summary job for {ticket_id}"
        )
        return

    from api.services.pipecat.transfer_context_handoff import _verify_credential_org

    if not await _verify_credential_org(config, organization_id):
        logger.warning(
            f"context_write: failed (stage=job:auth) — credential org mismatch "
            f"for ticket {ticket_id}"
        )
        return

    await _ensure_ticket_exists(config, snapshot)

    if snapshot.get("refer_status") != "success":
        # The caller never left the AI — mark the ticket instead of
        # summarizing it (D7: no ghost "transferred" tickets).
        await _append_note_with_retry(
            config,
            ticket_id,
            "transfer_failed",
            f"SIP REFER failed (status={snapshot.get('refer_status')}); "
            "caller stayed with the AI.",
        )
        return

    from api.services.configuration.ai_model_configuration import (
        get_effective_ai_model_configuration_for_workflow,
    )
    from api.services.pipecat.service_factory import create_llm_service
    from api.services.tickets.summarizer import generate_handoff_summary

    workflow = (
        await db_client.get_workflow_by_id(workflow_run.workflow_id)
        if workflow_run
        else None
    )
    user_id = workflow.user_id if workflow else None
    user_config = await get_effective_ai_model_configuration_for_workflow(
        user_id=user_id,
        organization_id=organization_id,
        workflow_configurations=run_configs,
    )
    llm = create_llm_service(user_config)

    summary = None
    last_error = None
    for attempt in (1, 2):  # at most one retry (D7)
        try:
            summary = await generate_handoff_summary(snapshot, llm)
            break
        except Exception as e:
            last_error = e
    if summary is None:
        logger.warning(
            f"handoff summary generation failed for ticket {ticket_id} "
            f"(run {workflow_run_id}): {last_error!r}; skeleton kept as-is"
        )
        return

    logger.info(
        f"handoff summary generated for ticket {ticket_id} (run {workflow_run_id}, "
        f"model={getattr(llm, 'model_name', 'unknown')})"
    )
    await _append_note_with_retry(config, ticket_id, "summary", summary)
