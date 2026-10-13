"""Call-time half of the restricted kb MCP tool (kb-service 3.3c, 3.5).

Three call-time consumers see ``mcp`` tools: session opening
(``PipecatEngine._open_mcp_sessions``), advertisement and handler registration
(``CustomToolManager``). When ``mcp`` is *restricted* in the canon each of them
compares the tool against the fixed definition here before going further:

- session opening checks the definition **and** the credential, and admits at
  most one tool per call; a rejected tool gets no session, so it is neither
  advertised nor registered;
- advertisement and registration re-check the definition (pure, no DB) so a
  session that somehow exists for a non-matching row is still not exposed.

What the LLM sees is dograh's own: the advertised schema is always
:func:`search_schema` (never the server's ``tools/list`` description or
properties), and the handler -- real or stub -- always runs behind the
``search_knowledge`` trust spec and output fence, in every mode.

Failure of the admitted tool is "no result", never dead air (C4): the handler
maps timeout and error to :func:`no_result`, and an admitted tool whose server
is down or does not list ``search_knowledge`` is offered as a stub that answers
with the same envelope -- so the LLM always has a result telling it to say it
cannot find the answer and offer a human agent, instead of answering from its
own knowledge or going quiet.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable, Optional

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema

from api.services.kb_tool import fixed
from api.services.observability.call_events import emit
from api.services.workflow.tools.mcp_tool import namespace_function_name

if TYPE_CHECKING:
    from api.services.workflow.mcp_tool_session import McpToolSession

#: Structured call event for a refused restricted tool (windowed alert).
DENIED_EVENT = "scope.restricted_tool_denied"

REASON_MISMATCH = "fixed_definition_mismatch"
REASON_MAX_INSTANCES = "max_instances"

NO_RESULT_TIMEOUT = "timeout"
NO_RESULT_ERROR = "error"
NO_RESULT_UNAVAILABLE = "unavailable"
NO_RESULT_TOOL_MISSING = "tool_missing"

_NO_RESULT_MESSAGE = (
    "The knowledge base could not be searched, so there is no answer to give. "
    "Do not answer from your own knowledge. Tell the caller you cannot find "
    "this information right now and offer to transfer them to a human agent."
)


def no_result(reason: str) -> dict:
    """The envelope the LLM receives whenever a kb search could not run."""
    return {"status": "no_result", "reason": reason, "message": _NO_RESULT_MESSAGE}


def log_denied(
    tool: Any,
    reason: str,
    fields: list[str],
    *,
    event_context: Optional[dict] = None,
) -> None:
    """Observable refusal: one log line plus the call event (windowed alert).

    Field *names* only; ``!r`` on the tool name because it is write-plane
    controlled (same rule as ``log_scope_denied_tool``).
    """
    field = ",".join(fields) or None
    logger.bind(
        call_event=DENIED_EVENT,
        tool_category="mcp",
        tool_name=getattr(tool, "name", None),
        reason=reason,
        field=field,
    ).warning(
        f"{DENIED_EVENT} category='mcp' tool={getattr(tool, 'name', None)!r} "
        f"uuid={getattr(tool, 'tool_uuid', None)!r} reason={reason} "
        f"fields={field}: does not match the fixed kb definition; not opened, "
        f"not advertised, not registered (kb-service D4)"
    )
    if event_context is None:
        return
    try:
        emit(
            DENIED_EVENT,
            room_name=event_context.get("room_name") or "",
            workflow_run_id=event_context.get("workflow_run_id"),
            reason=reason,
            tool_name=getattr(tool, "name", None),
            field=field,
        )
    except Exception as e:  # pragma: no cover - telemetry must not break calls
        logger.warning(f"restricted tool deny emit failed: {e}")


def admit_for_session(
    tool: Any,
    cfg: dict,
    credential: Any,
    *,
    admitted_so_far: int,
    event_context: Optional[dict] = None,
) -> bool:
    """Session-opening check: definition + credential + at most one per call."""
    fields = fixed.config_mismatches(cfg) + fixed.credential_mismatches(credential)
    if fields:
        log_denied(tool, REASON_MISMATCH, fields, event_context=event_context)
        return False
    if admitted_so_far >= fixed.KB_MAX_MCP_TOOLS:
        log_denied(tool, REASON_MAX_INSTANCES, [], event_context=event_context)
        return False
    return True


def definition_admissible(tool: Any) -> bool:
    """Advertisement/registration re-check (definition only, no DB)."""
    fields = fixed.definition_mismatches(getattr(tool, "definition", None))
    if fields:
        log_denied(tool, REASON_MISMATCH, fields)
        return False
    return True


def stub_reason(session: Optional["McpToolSession"]) -> Optional[str]:
    """Why an *admitted* kb tool needs the unavailable stub, or None.

    ``session is None`` means the tool was refused at session opening (or never
    reached it): it gets nothing, not a stub -- refused means not registered.
    """
    if session is None:
        return None
    if not session.available:
        return NO_RESULT_UNAVAILABLE
    if not session.function_schemas({fixed.KB_SEARCH_TOOL}):
        return NO_RESULT_TOOL_MISSING
    return None


def stub_function_name(tool: Any) -> str:
    fallback = (getattr(tool, "tool_uuid", "") or "")[:8] or "server"
    return namespace_function_name(tool.name, fixed.KB_SEARCH_TOOL, fallback=fallback)


def search_schema(tool: Any) -> FunctionSchema:
    """The only schema ever advertised for the kb tool (live or stub).

    Program constants, not the kb server's ``tools/list``: the server-side
    description and properties go into the LLM prompt, and a replaced or
    compromised kb must not be able to steer the LLM through them.
    """
    return FunctionSchema(
        name=stub_function_name(tool),
        description="Search the customer-service knowledge base for FAQ answers.",
        properties={
            "query": {
                "type": "string",
                "description": "The caller's question, rephrased as a search query.",
                "maxLength": fixed.SEARCH_KNOWLEDGE_QUERY_MAX_LEN,
            }
        },
        required=["query"],
    )


def create_stub_handler(tool: Any, reason: str):
    async def kb_unavailable_handler(function_call_params) -> None:
        logger.warning(
            f"kb.search_unavailable tool={tool.name!r} reason={reason}: "
            f"answering with the no-result envelope"
        )
        await function_call_params.result_callback(no_result(reason))

    return kb_unavailable_handler


def create_search_handler(
    session: "McpToolSession",
    function_name: str,
    run_id_provider: Optional[Callable[[], Any]] = None,
):
    """Proxy a ``search_knowledge`` call; timeout and error become no_result.

    The deadline is below the pipeline ceiling (read timeout + 5) so a hung kb
    ends in this envelope, not in Pipecat's bare ``None`` timeout result.
    Arguments are not logged: ``query`` is caller speech (D12).

    ``workflow_run_id`` is set from ``run_id_provider`` in **every** mode. The
    guard in front of this handler has already bound it (and reported a forged
    value); setting it here too keeps the field platform-sourced even if the
    guard's platform lookup came back empty.
    """

    async def kb_search_handler(function_call_params) -> None:
        arguments = dict(function_call_params.arguments or {})
        run_id = run_id_provider() if run_id_provider is not None else None
        if run_id not in (None, ""):
            arguments["workflow_run_id"] = str(run_id)
        try:
            result = await asyncio.wait_for(
                session.call(function_name, arguments),
                timeout=fixed.KB_HANDLER_DEADLINE_SECS,
            )
        except TimeoutError:
            logger.warning(f"kb.search_timeout tool={function_name!r}")
            await function_call_params.result_callback(no_result(NO_RESULT_TIMEOUT))
            return
        except Exception as e:  # noqa: BLE001 -- any failure is "no result" (C4)
            logger.warning(
                f"kb.search_failed tool={function_name!r}: {type(e).__name__}"
            )
            await function_call_params.result_callback(no_result(NO_RESULT_ERROR))
            return
        await function_call_params.result_callback(result)

    return kb_search_handler


def warn_if_tool_missing(tool: Any, session: "McpToolSession") -> None:
    """Loud line at session opening when the admitted server lacks the tool."""
    reason = stub_reason(session)
    if reason == NO_RESULT_TOOL_MISSING:
        logger.error(
            f"kb.tool_missing tool={tool.name!r}: the kb MCP server does not "
            f"list '{fixed.KB_SEARCH_TOOL}'; FAQ questions get the no-result "
            f"envelope for this call"
        )
