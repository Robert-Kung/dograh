"""Tool trust boundary for LIVEKIT dialogue tools (S-L8-TRUST).

C6: caller voice/DTMF is untrusted input. On the LIVEKIT path every
LLM-triggerable tool must be declared here (deny-by-default) and every
LLM-supplied argument passes deterministic validation before the handler
runs — a socially-engineered LLM can at most call a declared tool with
legal arguments. Policy lives in code so changes go through review;
org/workflow config supplies only values (destinations, field lists),
never rules.

Inventory of LIVEKIT dialogue-triggerable tools (S-L8-TRUST task 1.1):

======================  ========  =====================================
tool                    tier      declaration
======================  ========  =====================================
node transition funcs   —         excluded: graph control flow, dynamic
                                  per-edge names, no data parameters
retrieve_from_          read      FAMILY "knowledge_base"
  knowledge_base
safe_calculator         read      FAMILY "calculator"
END_CALL tools          read      FAMILY "end_call"
TRANSFER_CALL tools     transfer  FAMILY "transfer_call" — handler takes
                                  destination from config only; LLM args
                                  are stripped (never rejected) + flagged
HTTP custom tools       write     FAMILY "http" — operator-authored
                                  schema counts as the param declaration
ticket MCP tools        r/w       MCP registry by raw (un-namespaced)
                                  server-side tool name
other MCP tools         —         denied until declared here
======================  ========  =====================================

Non-LIVEKIT modes (Twilio/ARI/webrtc/…) are untouched (C3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple

from loguru import logger

from api.services.observability.call_events import emit


def is_trust_enforced(engine) -> bool:
    """True when the engine runs a LIVEKIT call (deny-by-default applies).

    Compares the mode value directly so test doubles (MagicMock engines)
    without an explicit mode read as not-enforced instead of truthy.
    """
    from api.enums import WorkflowRunMode

    return getattr(engine, "_workflow_run_mode", None) == WorkflowRunMode.LIVEKIT.value


READ = "read"
WRITE = "write"
TRANSFER = "transfer"

# Global caps applied to any string value that has no dedicated rule.
GLOBAL_MAX_LEN = 2000
# C0/C1 control chars except \n and \t (legitimate in dictated text).
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

E164_PATTERN = r"^\+[1-9]\d{1,14}$"

# Platform-bound source keys (resolved by the caller of guard()).
SOURCE_WORKFLOW_RUN_ID = "workflow_run_id"
SOURCE_CALLER_E164 = "caller_e164"


class TrustViolation(Exception):
    def __init__(self, reason: str, param: Optional[str] = None):
        super().__init__(reason if param is None else f"{reason}: {param}")
        self.reason = reason
        self.param = param


@dataclass(frozen=True)
class ParamRule:
    max_length: int = GLOBAL_MAX_LEN
    pattern: Optional[str] = None
    allowed_values: Optional[frozenset] = None


@dataclass(frozen=True)
class ToolTrustSpec:
    tier: str
    param_rules: Mapping[str, ParamRule] = field(default_factory=dict)
    # param name -> platform source key; platform value overrides the LLM's.
    platform_bound: Mapping[str, str] = field(default_factory=dict)
    # HTTP custom tools: the operator-authored tool schema is the declaration.
    schema_params_allowed: bool = False
    # transfer_call: the handler ignores LLM args entirely (destination is
    # config-only), so undeclared args are stripped + flagged, not rejected —
    # a hallucinated arg must not fail a legitimate transfer.
    strip_undeclared: bool = False
    # Reserved precondition knob (org/workflow config may enable); the guard
    # carries it so policy is declared in one place. Default off.
    requires_confirmation: bool = False


FAMILY_TRUST: Dict[str, ToolTrustSpec] = {
    "knowledge_base": ToolTrustSpec(
        tier=READ,
        param_rules={"query": ParamRule(max_length=500)},
    ),
    "calculator": ToolTrustSpec(
        tier=READ,
        param_rules={"expression": ParamRule(max_length=200)},
    ),
    "end_call": ToolTrustSpec(
        tier=READ,
        param_rules={"reason": ParamRule(max_length=200)},
    ),
    "transfer_call": ToolTrustSpec(tier=TRANSFER, strip_undeclared=True),
    "http": ToolTrustSpec(tier=WRITE, schema_params_allowed=True),
}

# Keyed by *raw* server-side MCP tool name (stable across LLM namespacing).
MCP_TOOL_TRUST: Dict[str, ToolTrustSpec] = {
    "create_ticket": ToolTrustSpec(
        tier=WRITE,
        param_rules={
            "ticket_id": ParamRule(max_length=64),
            "workflow_run_id": ParamRule(max_length=32),
            "caller_number": ParamRule(max_length=20, pattern=E164_PATTERN),
            "room_name": ParamRule(max_length=128),
            "transfer_reason": ParamRule(max_length=64),
        },
        platform_bound={
            "workflow_run_id": SOURCE_WORKFLOW_RUN_ID,
            "caller_number": SOURCE_CALLER_E164,
        },
    ),
    "append_ticket_note": ToolTrustSpec(
        tier=WRITE,
        param_rules={
            "ticket_id": ParamRule(max_length=64),
            "note_type": ParamRule(max_length=32),
            "content": ParamRule(max_length=8000),
        },
    ),
    "get_ticket": ToolTrustSpec(
        tier=READ,
        param_rules={"ticket_id": ParamRule(max_length=64)},
    ),
    "find_tickets_by_caller": ToolTrustSpec(
        tier=READ,
        param_rules={
            "caller_number": ParamRule(max_length=20, pattern=E164_PATTERN),
            "limit": ParamRule(max_length=4),
        },
        platform_bound={"caller_number": SOURCE_CALLER_E164},
    ),
}


def resolve_family_spec(family: str) -> Optional[ToolTrustSpec]:
    return FAMILY_TRUST.get(family)


def resolve_mcp_spec(raw_tool_name: Optional[str]) -> Optional[ToolTrustSpec]:
    if raw_tool_name is None:
        return None
    return MCP_TOOL_TRUST.get(raw_tool_name)


def log_denied_tool(kind: str, name: str) -> None:
    """Deny-by-default hit: loud structured log at registration/advertisement."""
    logger.bind(call_event="trust.tool_denied", tool_kind=kind, tool_name=name).warning(
        f"trust.tool_denied kind={kind} tool={name}: no trust spec declared; "
        f"tool hidden from the LIVEKIT dialogue path (S-L8-TRUST deny-by-default)"
    )


def sanitize_text(value: str, max_length: int = GLOBAL_MAX_LEN) -> str:
    """Global-cap sanitization: strip control chars, truncate."""
    return _CONTROL_CHARS.sub("", value)[:max_length]


def sanitize_any(value: Any, max_length: int = GLOBAL_MAX_LEN) -> Any:
    if isinstance(value, str):
        return sanitize_text(value, max_length)
    if isinstance(value, dict):
        return {k: sanitize_any(v, max_length) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_any(v, max_length) for v in value]
    return value


def _check_rule(param: str, value: Any, rule: ParamRule) -> Any:
    if isinstance(value, str):
        cleaned = _CONTROL_CHARS.sub("", value)
        if len(cleaned) > rule.max_length:
            raise TrustViolation("param_too_long", param)
        if rule.pattern is not None and not re.match(rule.pattern, cleaned):
            raise TrustViolation("param_pattern_mismatch", param)
        if rule.allowed_values is not None and cleaned not in rule.allowed_values:
            raise TrustViolation("param_not_allowed", param)
        return cleaned
    if isinstance(value, (dict, list)):
        return sanitize_any(value, rule.max_length)
    return value


def validate_arguments(
    tool_name: str,
    spec: ToolTrustSpec,
    arguments: Optional[Dict[str, Any]],
    *,
    declared_params: Optional[set] = None,
    platform_values: Optional[Mapping[str, Any]] = None,
) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """Deterministic argument validation (never an LLM).

    Returns ``(validated_args, overridden_params, stripped_params)``;
    raises :class:`TrustViolation` on a reject. ``declared_params`` is the
    tool's own advertised schema (MCP discovery / operator-authored HTTP
    schema) intersected with the registry rules; registry-declared params
    always win (D7).
    """
    args = dict(arguments or {})
    overridden: List[str] = []
    stripped: List[str] = []

    for param, source in spec.platform_bound.items():
        platform_value = (platform_values or {}).get(source)
        if platform_value in (None, ""):
            continue
        if param in args and str(args[param]) != str(platform_value):
            overridden.append(param)
        args[param] = platform_value

    validated: Dict[str, Any] = {}
    for param, value in args.items():
        rule = spec.param_rules.get(param)
        if rule is not None:
            validated[param] = _check_rule(param, value, rule)
            continue
        if spec.schema_params_allowed and declared_params and param in declared_params:
            validated[param] = sanitize_any(value)
            continue
        # Undeclared param: tier-split intersection policy (D7).
        if spec.strip_undeclared:
            stripped.append(param)
            continue
        if spec.tier in (WRITE, TRANSFER):
            raise TrustViolation("undeclared_param", param)
        if declared_params is not None and param not in declared_params:
            raise TrustViolation("undeclared_param", param)
        validated[param] = sanitize_any(value)

    return validated, overridden, stripped


def guard(
    handler: Callable,
    spec: ToolTrustSpec,
    *,
    tool_name: str,
    declared_params: Optional[set] = None,
    platform_values_provider: Optional[Callable[[], Awaitable[Dict[str, Any]]]] = None,
    event_context_provider: Optional[Callable[[], Dict[str, Any]]] = None,
) -> Callable:
    """Wrap a tool handler with the deterministic trust boundary.

    Rejects reply to the LLM with the structured ``VALIDATION_FAILED``
    envelope so the conversation continues (C4 — never dead air); the
    call itself is never crashed by validation.
    """

    def _event_fields() -> Dict[str, Any]:
        ctx = event_context_provider() if event_context_provider else {}
        return {
            "room_name": ctx.get("room_name") or "",
            "workflow_run_id": ctx.get("workflow_run_id"),
        }

    async def guarded(function_call_params) -> None:
        platform_values: Dict[str, Any] = {}
        if platform_values_provider is not None:
            try:
                platform_values = await platform_values_provider()
            except Exception as e:
                logger.warning(f"platform values lookup failed for {tool_name}: {e}")

        try:
            validated, overridden, stripped = validate_arguments(
                tool_name,
                spec,
                function_call_params.arguments,
                declared_params=declared_params,
                platform_values=platform_values,
            )
        except TrustViolation as v:
            fields = _event_fields()
            emit(
                "trust.violation",
                reason=v.reason,
                tool_name=tool_name,
                param=v.param,
                **fields,
            )
            await function_call_params.result_callback(
                {
                    "status": "error",
                    "code": "VALIDATION_FAILED",
                    "message": (
                        f"Invalid arguments for {tool_name} ({v.reason}"
                        f"{': ' + v.param if v.param else ''}). Adjust the "
                        f"arguments and retry, or offer to transfer the "
                        f"caller to the service queue."
                    ),
                }
            )
            return

        if stripped:
            # Call proceeds (e.g. transfer_call ignores LLM args by design),
            # but repeated attempts are an attack signal — feed the window.
            emit(
                "trust.violation",
                reason="undeclared_params_stripped",
                tool_name=tool_name,
                param=",".join(stripped),
                **_event_fields(),
            )
        if overridden:
            emit(
                "trust.override",
                reason="platform_bound_override",
                tool_name=tool_name,
                param=",".join(overridden),
                **_event_fields(),
            )

        function_call_params.arguments = validated
        await handler(function_call_params)

    return guarded
