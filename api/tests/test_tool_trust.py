"""Unit tests for the S-L8-TRUST tool trust boundary (tool_trust.py)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from pipecat.adapters.schemas.function_schema import FunctionSchema

from api.enums import ToolCategory, WorkflowRunMode
from api.services.workflow import tool_trust
from api.services.workflow.pipecat_engine import PipecatEngine
from api.services.workflow.pipecat_engine_custom_tools import CustomToolManager
from api.services.workflow.tool_trust import (
    GLOBAL_MAX_LEN,
    ParamRule,
    ToolTrustSpec,
    TrustViolation,
    guard,
    is_trust_enforced,
    resolve_family_spec,
    resolve_mcp_spec,
    sanitize_any,
    validate_arguments,
)


class FakeCallParams:
    def __init__(self, arguments):
        self.arguments = arguments
        self.results = []

    async def result_callback(self, result, *, properties=None):
        self.results.append(result)


def _events(monkeypatch):
    captured = []

    def fake_emit(event, **fields):
        captured.append((event, fields))

    monkeypatch.setattr(tool_trust, "emit", fake_emit)
    return captured


# ---------------------------------------------------------------------------
# validate_arguments
# ---------------------------------------------------------------------------


def test_declared_rule_passes_and_strips_control_chars():
    spec = ToolTrustSpec(
        tier="write", param_rules={"content": ParamRule(max_length=50)}
    )
    validated, overridden, stripped = validate_arguments(
        "t", spec, {"content": "hel\x00lo\x1b"}
    )
    assert validated == {"content": "hello"}
    assert overridden == [] and stripped == []


def test_too_long_rejected():
    spec = ToolTrustSpec(tier="write", param_rules={"content": ParamRule(max_length=5)})
    with pytest.raises(TrustViolation) as exc:
        validate_arguments("t", spec, {"content": "x" * 6})
    assert exc.value.reason == "param_too_long"


def test_pattern_mismatch_rejected():
    spec = ToolTrustSpec(
        tier="read",
        param_rules={"n": ParamRule(max_length=20, pattern=tool_trust.E164_PATTERN)},
    )
    with pytest.raises(TrustViolation) as exc:
        validate_arguments("t", spec, {"n": "not-a-number"})
    assert exc.value.reason == "param_pattern_mismatch"


def test_write_tier_undeclared_param_rejected():
    spec = resolve_mcp_spec("create_ticket")
    with pytest.raises(TrustViolation) as exc:
        validate_arguments("create_ticket", spec, {"status": "resolved"})
    assert exc.value.reason == "undeclared_param"
    assert exc.value.param == "status"


def test_read_tier_undeclared_param_capped_not_rejected():
    spec = ToolTrustSpec(tier="read", param_rules={})
    validated, _, _ = validate_arguments("t", spec, {"q": "a\x00" + "b" * 3000})
    assert validated["q"] == "a" + "b" * (GLOBAL_MAX_LEN - 1)


def test_read_tier_outside_discovered_schema_rejected():
    spec = ToolTrustSpec(tier="read", param_rules={})
    with pytest.raises(TrustViolation):
        validate_arguments("t", spec, {"q": "x"}, declared_params={"other"})


def test_transfer_strip_undeclared_flags_but_never_rejects():
    spec = resolve_family_spec("transfer_call")
    validated, overridden, stripped = validate_arguments(
        "transfer_x", spec, {"destination": "+886900111222", "note": "hi"}
    )
    assert validated == {}
    assert sorted(stripped) == ["destination", "note"]
    assert overridden == []


def test_platform_bound_overrides_llm_value():
    spec = resolve_mcp_spec("find_tickets_by_caller")
    validated, overridden, _ = validate_arguments(
        "find_tickets_by_caller",
        spec,
        {"caller_number": "+886911111111"},
        platform_values={"caller_e164": "+886922222222"},
    )
    assert validated["caller_number"] == "+886922222222"
    assert overridden == ["caller_number"]


def test_platform_bound_missing_platform_value_passes_through():
    spec = resolve_mcp_spec("find_tickets_by_caller")
    validated, overridden, _ = validate_arguments(
        "find_tickets_by_caller",
        spec,
        {"caller_number": "+886911111111"},
        platform_values={"caller_e164": ""},
    )
    assert validated["caller_number"] == "+886911111111"
    assert overridden == []


def test_http_schema_params_allowed_with_global_caps():
    spec = resolve_family_spec("http")
    validated, _, _ = validate_arguments(
        "lookup_order",
        spec,
        {"order_id": "A123", "note": "x" * 3000},
        declared_params={"order_id", "note"},
    )
    assert validated["order_id"] == "A123"
    assert len(validated["note"]) == GLOBAL_MAX_LEN
    with pytest.raises(TrustViolation):
        validate_arguments(
            "lookup_order", spec, {"evil": "1"}, declared_params={"order_id"}
        )


def test_sanitize_any_recurses():
    out = sanitize_any({"a": ["x\x00", {"b": "y\x1b"}]})
    assert out == {"a": ["x", {"b": "y"}]}


# ---------------------------------------------------------------------------
# guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guard_rejects_with_validation_failed_envelope(monkeypatch):
    events = _events(monkeypatch)
    handler = AsyncMock()
    spec = resolve_mcp_spec("create_ticket")
    guarded = guard(handler, spec, tool_name="create_ticket")

    params = FakeCallParams({"status": "resolved"})
    await guarded(params)

    handler.assert_not_awaited()
    assert params.results[0]["code"] == "VALIDATION_FAILED"
    assert params.results[0]["status"] == "error"
    assert events[0][0] == "trust.violation"
    assert events[0][1]["reason"] == "undeclared_param"


@pytest.mark.asyncio
async def test_guard_passes_validated_args_to_handler(monkeypatch):
    _events(monkeypatch)
    seen = {}

    async def handler(p):
        seen["args"] = p.arguments

    spec = resolve_mcp_spec("append_ticket_note")
    guarded = guard(handler, spec, tool_name="append_ticket_note")
    await guarded(
        FakeCallParams({"ticket_id": "T-1", "note_type": "event", "content": "ok\x00"})
    )
    assert seen["args"] == {"ticket_id": "T-1", "note_type": "event", "content": "ok"}


@pytest.mark.asyncio
async def test_guard_emits_override_event(monkeypatch):
    events = _events(monkeypatch)
    handler = AsyncMock()
    spec = resolve_mcp_spec("find_tickets_by_caller")

    async def platform_values():
        return {"caller_e164": "+886922222222"}

    guarded = guard(
        handler,
        spec,
        tool_name="find_tickets_by_caller",
        platform_values_provider=platform_values,
        event_context_provider=lambda: {"room_name": "r1", "workflow_run_id": 7},
    )
    await guarded(FakeCallParams({"caller_number": "+886911111111"}))

    handler.assert_awaited_once()
    assert events == [
        (
            "trust.override",
            {
                "reason": "platform_bound_override",
                "tool_name": "find_tickets_by_caller",
                "param": "caller_number",
                "room_name": "r1",
                "workflow_run_id": 7,
            },
        )
    ]


@pytest.mark.asyncio
async def test_guard_platform_provider_failure_does_not_crash(monkeypatch):
    _events(monkeypatch)
    handler = AsyncMock()

    async def broken():
        raise RuntimeError("boom")

    spec = resolve_mcp_spec("get_ticket")
    guarded = guard(
        handler, spec, tool_name="get_ticket", platform_values_provider=broken
    )
    await guarded(FakeCallParams({"ticket_id": "T-1"}))
    handler.assert_awaited_once()


# ---------------------------------------------------------------------------
# is_trust_enforced / registration gating
# ---------------------------------------------------------------------------


def test_is_trust_enforced_mode_comparison():
    engine = MagicMock()
    assert is_trust_enforced(engine) is False  # MagicMock attr != "livekit"
    engine._workflow_run_mode = WorkflowRunMode.LIVEKIT.value
    assert is_trust_enforced(engine) is True


def _tool(category, name="My Tool"):
    t = MagicMock()
    t.tool_uuid = "uuid-1"
    t.name = name
    t.category = category
    t.definition = {"config": {}}
    return t


def _livekit_manager(tools, monkeypatch, mcp_sessions=None):
    engine = MagicMock()
    engine._workflow_run_mode = WorkflowRunMode.LIVEKIT.value
    engine._mcp_sessions = mcp_sessions or {}
    engine.trust_event_context = lambda: {"room_name": "r", "workflow_run_id": 1}
    engine.get_platform_bound_values = AsyncMock(return_value={})
    registered = {}
    engine.llm.register_function = lambda name, fn, **kw: registered.__setitem__(
        name, fn
    )
    mgr = CustomToolManager(engine)
    mgr.get_organization_id = AsyncMock(return_value=42)
    from api.db import db_client

    monkeypatch.setattr(db_client, "get_tools_by_uuids", AsyncMock(return_value=tools))
    return mgr, registered


def _fake_mcp_session(schemas, raw_names):
    session = MagicMock()
    session.available = True
    session.call_timeout_secs = 10.0
    session.function_schemas = lambda allowed=None: list(schemas)
    session.raw_name = lambda name: raw_names.get(name)
    return session


@pytest.mark.asyncio
async def test_livekit_denies_undeclared_mcp_tool_both_faces(monkeypatch):
    declared = FunctionSchema(
        name="mcp__t__create_ticket",
        description="",
        properties={"ticket_id": {"type": "string"}},
        required=[],
    )
    undeclared = FunctionSchema(
        name="mcp__t__delete_everything", description="", properties={}, required=[]
    )
    session = _fake_mcp_session(
        [declared, undeclared],
        {
            "mcp__t__create_ticket": "create_ticket",
            "mcp__t__delete_everything": "delete_everything",
        },
    )
    tool = _tool(ToolCategory.MCP.value)
    mgr, registered = _livekit_manager(
        [tool], monkeypatch, mcp_sessions={"uuid-1": session}
    )

    schemas = await mgr.get_tool_schemas(["uuid-1"])
    assert [s.name for s in schemas] == ["mcp__t__create_ticket"]

    await mgr.register_handlers(["uuid-1"])
    assert "mcp__t__create_ticket" in registered
    assert "mcp__t__delete_everything" not in registered


@pytest.mark.asyncio
async def test_non_livekit_mode_unchanged(monkeypatch):
    undeclared = FunctionSchema(
        name="mcp__t__anything", description="", properties={}, required=[]
    )
    session = _fake_mcp_session([undeclared], {"mcp__t__anything": "anything"})
    tool = _tool(ToolCategory.MCP.value)
    mgr, registered = _livekit_manager(
        [tool], monkeypatch, mcp_sessions={"uuid-1": session}
    )
    mgr._engine._workflow_run_mode = WorkflowRunMode.TWILIO.value

    schemas = await mgr.get_tool_schemas(["uuid-1"])
    assert [s.name for s in schemas] == ["mcp__t__anything"]
    await mgr.register_handlers(["uuid-1"])
    assert "mcp__t__anything" in registered


@pytest.mark.asyncio
async def test_livekit_declared_families_still_register(monkeypatch):
    tool = _tool(ToolCategory.END_CALL.value, name="End Call")
    mgr, registered = _livekit_manager([tool], monkeypatch)
    await mgr.register_handlers(["uuid-1"])
    assert "end_call" in registered


# ---------------------------------------------------------------------------
# dormant registration path lockdown (pipecat side)
# ---------------------------------------------------------------------------


def _livekit_engine():
    return PipecatEngine(
        workflow=MagicMock(),
        call_context_vars={},
        workflow_run_mode=WorkflowRunMode.LIVEKIT.value,
        room_name="room-1",
    )


def test_handler_carrying_schema_fails_fast():
    engine = _livekit_engine()
    engine.llm = MagicMock()
    engine.llm._functions = {}
    schema = MagicMock()
    schema.handler = lambda: None
    schema.name = "sneaky"
    with pytest.raises(RuntimeError, match="S-L8-TRUST"):
        engine._assert_no_dormant_registration_paths([schema])


def test_catch_all_registration_fails_fast():
    engine = _livekit_engine()
    engine.llm = MagicMock()
    engine.llm._functions = {None: object()}
    schema = FunctionSchema(name="ok", description="", properties={}, required=[])
    with pytest.raises(RuntimeError, match="catch-all"):
        engine._assert_no_dormant_registration_paths([schema])


def test_clean_schemas_pass_lockdown():
    engine = _livekit_engine()
    engine.llm = MagicMock()
    engine.llm._functions = {"ok": object()}
    schema = FunctionSchema(name="ok", description="", properties={}, required=[])
    engine._assert_no_dormant_registration_paths([schema])


# ---------------------------------------------------------------------------
# template interpolation bypass (gathered_context -> HTTP preset params)
# ---------------------------------------------------------------------------


def test_preset_parameters_sanitized_when_untrusted():
    from api.services.workflow.tools.custom_tool import _resolve_preset_parameters

    config = {
        "preset_parameters": [
            {
                "name": "note",
                "type": "string",
                "value_template": "{{gathered_context.note}}",
            }
        ]
    }
    gathered = {"note": "inject\x00me" + "x" * 3000}
    out = _resolve_preset_parameters(config, {}, gathered, sanitize_untrusted=True)
    assert "\x00" not in out["note"]
    assert len(out["note"]) <= GLOBAL_MAX_LEN

    untouched = _resolve_preset_parameters(config, {}, gathered)
    assert untouched["note"] == gathered["note"]
