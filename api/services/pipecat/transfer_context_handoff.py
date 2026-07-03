"""Transfer context handoff (S-L4-SCREENPOP).

At the moment the shared cold-transfer preamble decides to REFER, this
module: generates the ticket correlation id locally (deterministic per
workflow run), returns the REFER headers that attach it to the call leg,
snapshots the conversation material in-memory, fires the skeleton ticket
write in the background, and — once the REFER outcome is known — enqueues
the ARQ summary job. Nothing here ever blocks or fails the transfer (C4):
every failure path degrades to a `context_write: failed` log marker plus
a counter, and the REFER proceeds untouched.

Writes go through the ticket MCP contract over streamable HTTP — never an
in-process service call — so swapping the server for an owner's CRM
wrapper requires no agent change (C5/D3). Sessions are opened and closed
inside a single task per operation (anyio cancel scopes are task-affine;
see run_pipeline.py MCP teardown).
"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Optional

from loguru import logger

from api.db import db_client
from api.services.tickets import contract
from api.services.tickets.config import TicketServerConfig, resolve_ticket_server_config
from api.services.tickets.sanitize import clean_text

# Both header spellings are attached: User-to-User is the SIP/ACD standard
# for attached data (UUI), the X- header survives trunks that rewrite UUI.
# Which one reaches the transferee is trunk-dependent (§0.2 live-deferred);
# phone-number lookup stays the documented fallback channel either way.
UUI_HEADER = "User-to-User"
TICKET_HEADER = "X-Dograh-Ticket-Id"

# Snapshot caps: the summary prompt doesn't need unbounded history, and the
# snapshot travels through the ARQ queue.
SNAPSHOT_MAX_MESSAGES = 60
SNAPSHOT_MAX_TEXT_LEN = 2000

# S-L7-OBS wiring point: process-local failure counter + the structured
# "context_write: failed" log marker emitted by _record_write_failure.
CONTEXT_WRITE_METRICS = {"failed": 0}

# Keep strong references so fire-and-forget writes aren't GC'd mid-flight.
_background_tasks: set = set()


def _record_write_failure(stage: str, detail: str) -> None:
    CONTEXT_WRITE_METRICS["failed"] += 1
    logger.warning(f"context_write: failed (stage={stage}) — {detail}")


@dataclass
class HandoffPlan:
    """Everything decided at REFER time; carried to the ARQ job as a dict."""

    config: TicketServerConfig
    ticket_id: str
    workflow_run_id: int
    organization_id: int
    caller_number: str
    room_name: str
    transfer_reason: str
    snapshot_messages: list = field(default_factory=list)
    gathered_context: dict = field(default_factory=dict)

    @property
    def refer_headers(self) -> dict[str, str]:
        return {
            UUI_HEADER: f"{self.ticket_id};encoding=ascii",
            TICKET_HEADER: self.ticket_id,
        }

    def to_job_snapshot(self, refer_status: str) -> dict:
        return {
            "ticket_id": self.ticket_id,
            "workflow_run_id": self.workflow_run_id,
            "organization_id": self.organization_id,
            "transfer_reason": self.transfer_reason,
            "refer_status": refer_status,
            "messages": self.snapshot_messages,
            "gathered_context": self.gathered_context,
        }


def snapshot_messages(context) -> list[dict]:
    """Flatten LLM context messages to JSON-safe {role, content} pairs.

    Text parts only — binary/audio is already placeholder-substituted by
    `truncate_large_values`, and the summarizer needs prose, not payloads.
    """
    if context is None:
        return []
    try:
        raw = context.get_messages(truncate_large_values=True)
    except Exception:
        return []
    out = []
    for msg in raw[-SNAPSHOT_MAX_MESSAGES:]:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        if not isinstance(content, str) or role not in ("user", "assistant", "system"):
            continue
        content = content.strip()
        if content:
            out.append({"role": role, "content": content[:SNAPSHOT_MAX_TEXT_LEN]})
    return out


async def get_caller_number(room_name: str, lk) -> str:
    """E.164 caller number from the SIP participant's attributes, or ""
    (anonymous / unparseable / lookup failure are all the same legal shape)."""
    from livekit.protocol.models import ParticipantInfo
    from livekit.protocol.room import ListParticipantsRequest

    from api.utils.telephony_address import normalize_telephony_address

    try:
        resp = await lk.room.list_participants(ListParticipantsRequest(room=room_name))
        for p in resp.participants:
            if p.kind == ParticipantInfo.Kind.SIP:
                raw = p.attributes.get("sip.phoneNumber", "")
                if not raw:
                    return ""
                normalized = normalize_telephony_address(raw)
                if normalized.address_type == "pstn":
                    return normalized.canonical
                return ""
    except Exception as e:
        logger.debug(f"caller number lookup failed for {room_name}: {e}")
    return ""


async def _verify_credential_org(config: TicketServerConfig, org_id: int) -> bool:
    """D3 write-time guard: a credential that resolves to a *different* Dograh
    org is a misconfiguration — refuse to write. Keys unknown to the platform
    (external single-tenant wrappers) cannot be validated and pass through."""
    try:
        api_key_model = await db_client.validate_api_key(config.api_key)
    except Exception:
        return True  # validation infra down must not block the write path
    if api_key_model is None:
        return True
    return api_key_model.organization_id == org_id


async def call_ticket_tool(
    config: TicketServerConfig, tool: str, arguments: dict
) -> Any:
    """One MCP tool call over an ephemeral streamable-HTTP session.

    Opened and closed within the calling task. Raises on transport errors
    and timeouts; returns the parsed tool result (contract error envelopes
    come back as plain dicts, not exceptions).
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def _call():
        async with streamablehttp_client(
            config.url,
            headers={"Authorization": f"Bearer {config.api_key}"},
            timeout=timedelta(seconds=config.timeout_seconds),
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool, arguments=arguments)
                if getattr(result, "structuredContent", None):
                    return result.structuredContent
                for content in result.content or []:
                    text = getattr(content, "text", None)
                    if text:
                        try:
                            return json.loads(text)
                        except ValueError:
                            return {"raw": text}
                return {}

    # Overall cap so a stalled connect/read can never exceed the configured
    # budget regardless of which inner timeout misbehaves.
    return await asyncio.wait_for(_call(), timeout=config.timeout_seconds * 2)


async def prepare_transfer_handoff(
    engine, *, room_name: str, transfer_reason: str, lk=None
) -> Optional[HandoffPlan]:
    """Resolve config, snapshot, and launch the skeleton write (background).

    Returns None — and touches nothing — when no ticket server is configured
    (D5: the transfer behaves exactly as before this capability). Never
    raises (C4).
    """
    try:
        workflow_run_id = getattr(engine, "_workflow_run_id", None)
        if not workflow_run_id:
            return None
        organization_id = await engine._get_organization_id()
        if organization_id is None:
            return None

        # get_workflow_run selectinloads .definition; the *_by_id variant
        # doesn't, and lazy-loading outside the session raises in async SQLA.
        workflow_run = await db_client.get_workflow_run(workflow_run_id)
        run_configs = (
            (workflow_run.definition.workflow_configurations or {})
            if workflow_run and workflow_run.definition
            else {}
        )
        config = await resolve_ticket_server_config(organization_id, run_configs)
        if config is None:
            return None

        plan = HandoffPlan(
            config=config,
            ticket_id=contract.ticket_id_for_run(workflow_run_id),
            workflow_run_id=workflow_run_id,
            organization_id=organization_id,
            caller_number="",
            room_name=clean_text(room_name or "", contract.ROOM_NAME_MAX_LEN),
            transfer_reason=clean_text(
                transfer_reason or "unknown", contract.TRANSFER_REASON_MAX_LEN
            ),
            snapshot_messages=snapshot_messages(getattr(engine, "context", None)),
            gathered_context=dict(getattr(engine, "_gathered_context", None) or {}),
        )

        task = asyncio.create_task(_write_skeleton(plan, lk=lk))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return plan
    except Exception as e:
        _record_write_failure("prepare", repr(e))
        return None


async def _write_skeleton(plan: HandoffPlan, lk=None) -> None:
    """Fire-and-forget skeleton create — off the transfer's critical path."""
    own = lk is None
    try:
        if not await _verify_credential_org(plan.config, plan.organization_id):
            _record_write_failure(
                "skeleton",
                f"credential org mismatch for workflow org {plan.organization_id}",
            )
            return

        if own:
            import os

            from livekit import api as lk_api

            lk = lk_api.LiveKitAPI(
                url=os.environ["LIVEKIT_URL"],
                api_key=os.environ["LIVEKIT_API_KEY"],
                api_secret=os.environ["LIVEKIT_API_SECRET"],
            )
        plan.caller_number = await get_caller_number(plan.room_name, lk)

        result = await call_ticket_tool(
            plan.config,
            "create_ticket",
            {
                "ticket_id": plan.ticket_id,
                "workflow_run_id": plan.workflow_run_id,
                "caller_number": plan.caller_number,
                "room_name": plan.room_name,
                "transfer_reason": plan.transfer_reason,
            },
        )
        if contract.is_error(result):
            _record_write_failure("skeleton", f"server rejected: {result['error']}")
    except Exception as e:
        _record_write_failure("skeleton", repr(e))
    finally:
        if own and lk is not None:
            try:
                await lk.aclose()
            except Exception:
                pass


async def finalize_transfer_handoff(plan: HandoffPlan, refer_status: str) -> None:
    """Enqueue the summary/failure-note ARQ job once the REFER outcome is
    known. Never raises (C4); a lost enqueue leaves the skeleton in place."""
    try:
        from api.tasks.arq import enqueue_job
        from api.tasks.function_names import FunctionNames

        await enqueue_job(
            FunctionNames.SUMMARIZE_TRANSFER_HANDOFF,
            plan.to_job_snapshot(refer_status),
        )
    except Exception as e:
        _record_write_failure("enqueue", repr(e))
