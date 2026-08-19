"""W2a: the platform enabled set and the shared REFER parser, at call time.

Four things are pinned here, one per failure this change exists to close:

1. **The shared parser is one rule, not three copies.** The premium-rate guard
   and the write-path validator both read
   ``deploy/bin/testdata/uri/vectors.json`` — the same file the platform repo's
   own tests read. A vector that disagrees fails on both sides of the repo
   boundary rather than in neither.
2. **The enabled-set filter is unconditional.** Specifically it must hold in
   text-chat mode, where ``is_trust_enforced`` is False — that is the one path
   a customer identity can drive itself.
3. **Canon unreadable ⇒ nothing governed is registered** (fail-closed).
4. **Transfer config is re-checked on read**, not only on write (issue #3).

Tests in group 1 need the real parser and skip without it; see
``support/platform_artifacts.py`` for why that asymmetry exists.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from api.enums import ToolCategory, WorkflowRunMode
from api.services import platform_scope
from api.services.workflow.pipecat_engine_custom_tools import CustomToolManager
from api.tests.support.platform_artifacts import (
    DELIVERED_SCOPE,
    requires_sip_uri,
    sip_uri_path,
)

VECTORS_PATH = Path(
    os.environ.get("PLATFORM_URI_VECTORS") or "/opt/platform/vectors.json"
)


def _vectors():
    """The platform repo's hand-written vectors, or an empty list.

    Read lazily and tolerantly: the file travels with ``sip_uri.py`` and the
    skipif marker on each test is what actually reports its absence.
    """
    if not VECTORS_PATH.is_file():
        return []
    raw = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
    return raw["vectors"] if isinstance(raw, dict) else raw


VECTORS = _vectors()
ACCEPTED = [v for v in VECTORS if v.get("expect", {}).get("ok")]
REJECTED = [v for v in VECTORS if not v.get("expect", {}).get("ok")]


# ── 1. one parser, shared across the repo boundary ────────────────────────


@requires_sip_uri
def test_vectors_file_is_actually_present_and_non_trivial():
    """A skipped-away suite is the failure mode this guards against.

    If the parser is mounted, the vectors must be too — otherwise every
    parametrised test below silently collapses to zero cases and reads green.
    """
    assert VECTORS, (
        f"{sip_uri_path()} is present but {VECTORS_PATH} is not; the shared "
        "vectors must travel with the shared parser"
    )
    assert ACCEPTED and REJECTED


@requires_sip_uri
@pytest.mark.parametrize("vector", REJECTED, ids=lambda v: v["value"][:40])
def test_write_path_rejects_every_rejected_vector(vector):
    """``validate_destination`` is the shared rule, with nothing added."""
    from api.schemas.tool import TransferCallConfig

    with pytest.raises(ValidationError):
        TransferCallConfig(destination=vector["value"])


@requires_sip_uri
@pytest.mark.parametrize("vector", ACCEPTED, ids=lambda v: v["value"][:40])
def test_write_path_accepts_every_accepted_vector(vector):
    """...and nothing removed either — over-blocking here means unwritable
    delivery-state config, which is how ``tel:`` nearly got broken (D-A2)."""
    from api.schemas.tool import TransferCallConfig

    value = vector["value"]
    # Premium-rate prefixes are a separate guard, not a shape rule; a vector
    # that is shape-valid but premium-rate is rejected for the other reason.
    from api.services.pipecat.capacity_gate import _premium_rate

    if _premium_rate(value):
        pytest.skip("shape-valid but premium-rate; covered by the guard tests")
    assert TransferCallConfig(destination=value).destination == value


@requires_sip_uri
def test_premium_rate_now_covers_the_sip_host():
    """issue #4: the host is what gets dialled, and it was unguarded."""
    from api.services.pipecat.capacity_gate import _premium_rate

    assert _premium_rate("sip:queue@886204.example") is True
    assert _premium_rate("sip:1900@pbx.example") is True  # user part, as before
    assert _premium_rate("tel:+19005551234") is True
    assert _premium_rate("sip:queue@pbx.example") is False


@requires_sip_uri
def test_multiple_at_is_rejected_on_the_write_path():
    """AC1, from this side of the boundary."""
    from api.schemas.tool import TransferCallConfig

    with pytest.raises(ValidationError):
        TransferCallConfig(destination="sip:a@b@evil.com")


def test_write_path_refuses_when_the_parser_is_unmounted(monkeypatch):
    """Fail-closed, and specifically *not* an unhandled ImportError (D-A5)."""
    from api.schemas.tool import TransferCallConfig

    monkeypatch.setenv("PLATFORM_SIP_URI", "/nonexistent/sip_uri.py")
    platform_scope.reset_cache()

    with pytest.raises(ValidationError) as excinfo:
        TransferCallConfig(destination="tel:+886223456789")
    assert "not available in this container" in str(excinfo.value)


def test_premium_guard_degrades_instead_of_raising_when_unmounted(monkeypatch):
    """The opposite choice from the write path, deliberately.

    Raising here would take ``validate_capacity_config`` down at startup — a
    missing ``-v`` would stop the platform answering the phone at all. The
    fallback is exactly the pre-W2a check: user part only, host unguarded.
    """
    from api.services.pipecat.capacity_gate import _premium_rate

    monkeypatch.setenv("PLATFORM_SIP_URI", "/nonexistent/sip_uri.py")
    platform_scope.reset_cache()

    assert _premium_rate("sip:1900@pbx.example") is True
    assert _premium_rate("sip:queue@886204.example") is False  # the lost coverage
    assert _premium_rate("tel:+886223456789") is False


# ── 2/3. the call-time enabled-set filter ─────────────────────────────────


def _tool(name, category, definition=None):
    t = MagicMock()
    t.tool_uuid = f"uuid-{name}"
    t.name = name
    t.category = category
    t.definition = definition if definition is not None else {"config": {}}
    return t


def _manager(tools, monkeypatch, *, mode):
    engine = MagicMock()
    engine._workflow_run_mode = mode
    engine._mcp_sessions = {}
    engine.trust_event_context = lambda: {"room_name": "r", "workflow_run_id": 1}
    engine.get_platform_bound_values = AsyncMock(return_value={})
    registered: dict = {}
    engine.llm.register_function = lambda name, fn, **kw: registered.__setitem__(
        name, fn
    )
    mgr = CustomToolManager(engine)
    mgr.get_organization_id = AsyncMock(return_value=42)
    from api.db import db_client

    monkeypatch.setattr(db_client, "get_tools_by_uuids", AsyncMock(return_value=tools))
    return mgr, registered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [WorkflowRunMode.LIVEKIT.value, WorkflowRunMode.TEXTCHAT.value],
    ids=["livekit", "text-chat"],
)
async def test_enabled_set_filter_holds_in_both_modes(monkeypatch, mode):
    """4.2: the filter must **not** sit inside ``if trust:``.

    ``is_trust_enforced`` is True only in LIVEKIT mode, and text-chat is the
    one path a customer identity can drive itself. A filter that only ran under
    trust would leave the enabled set unenforced exactly where it is reachable.
    """
    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(DELIVERED_SCOPE))
    platform_scope.reset_cache()

    tools = [
        _tool("calc", ToolCategory.CALCULATOR.value),
        _tool("hangup", ToolCategory.END_CALL.value),
    ]
    mgr, registered = _manager(tools, monkeypatch, mode=mode)

    schemas = await mgr.get_tool_schemas(["uuid-calc", "uuid-hangup"])
    assert not any("calculator" in s.name for s in schemas)

    await mgr.register_handlers(["uuid-calc", "uuid-hangup"])
    assert not any("calculator" in name for name in registered)


@pytest.mark.asyncio
async def test_enabled_set_filter_lets_allowed_categories_through(monkeypatch):
    """The filter is a filter, not an off switch — end_call still registers."""
    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(DELIVERED_SCOPE))
    platform_scope.reset_cache()

    mgr, _ = _manager(
        [_tool("hangup", ToolCategory.END_CALL.value)],
        monkeypatch,
        mode=WorkflowRunMode.LIVEKIT.value,
    )
    schemas = await mgr.get_tool_schemas(["uuid-hangup"])
    assert schemas, "end_call is in the delivered enabled set and must survive"


@pytest.mark.asyncio
async def test_canon_unreadable_registers_nothing(monkeypatch):
    """4.4: fail-closed. The call continues; the governed tools do not."""
    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", "/nonexistent/feature-scope.json")
    platform_scope.reset_cache()

    tools = [
        _tool("hangup", ToolCategory.END_CALL.value),
        _tool("xfer", ToolCategory.TRANSFER_CALL.value),
    ]
    mgr, registered = _manager(tools, monkeypatch, mode=WorkflowRunMode.LIVEKIT.value)

    assert await mgr.get_tool_schemas(["uuid-hangup", "uuid-xfer"]) == []
    await mgr.register_handlers(["uuid-hangup", "uuid-xfer"])
    assert registered == {}


@pytest.mark.asyncio
async def test_empty_allowed_list_is_treated_as_unreadable(monkeypatch, tmp_path):
    """An empty allowlist is a broken canon, not 'allow nothing on purpose'."""
    canon = tmp_path / "scope.json"
    canon.write_text(json.dumps({"allowed_tool_types": []}), encoding="utf-8")
    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(canon))
    platform_scope.reset_cache()

    with pytest.raises(platform_scope.PlatformArtifactMissing):
        platform_scope.allowed_tool_categories()


# ── 4. transfer config re-validated on read (issue #3) ────────────────────


def _workflow_with_transfer_tool():
    node = MagicMock()
    node.tool_uuids = ["uuid-xfer"]
    workflow = MagicMock()
    workflow.nodes = {"1": node}
    return workflow


async def _resolve(config, monkeypatch):
    from api.db import db_client
    from api.services.pipecat.transfer_call_config import find_transfer_call_config

    tool = _tool("xfer", ToolCategory.TRANSFER_CALL.value, {"config": config})
    monkeypatch.setattr(db_client, "get_tools_by_uuids", AsyncMock(return_value=[tool]))
    return await find_transfer_call_config(_workflow_with_transfer_tool(), 42)


@requires_sip_uri
@pytest.mark.asyncio
async def test_read_path_rejects_a_destination_the_write_path_would_have(monkeypatch):
    """The row can predate the rule, or be written by a path that bypassed it.

    Returning None routes callers into their existing no-config path, which
    since W0 emits ``transfer.failed`` rather than silently not installing.
    """
    assert await _resolve({"destination": "sip:a@b@evil.com"}, monkeypatch) is None
    assert (
        await _resolve({"destination": "SIP/human-queue@10.0.0.1"}, monkeypatch) is None
    )


@requires_sip_uri
@pytest.mark.asyncio
async def test_read_path_keeps_a_valid_destination(monkeypatch):
    config = await _resolve({"destination": "tel:+886223456789"}, monkeypatch)
    assert config["destination"] == "tel:+886223456789"


@requires_sip_uri
@pytest.mark.asyncio
async def test_bad_alternate_is_dropped_without_killing_the_main_path(monkeypatch):
    """Blast radius per field: the after-hours branch dies, the transfer lives."""
    config = await _resolve(
        {"destination": "tel:+886223456789", "alternateDestination": "SIP/x"},
        monkeypatch,
    )
    assert config["destination"] == "tel:+886223456789"
    assert "alternateDestination" not in config


@requires_sip_uri
@pytest.mark.asyncio
async def test_bad_health_url_drops_the_health_keys_only(monkeypatch):
    config = await _resolve(
        {
            "destination": "tel:+886223456789",
            "queueHealthUrl": "http://user:pw@queue:8080/health",
            "queueHealthToken": "t",
        },
        monkeypatch,
    )
    assert config["destination"] == "tel:+886223456789"
    assert "queueHealthUrl" not in config
    assert "queueHealthToken" not in config


@requires_sip_uri
@pytest.mark.asyncio
async def test_good_health_url_survives(monkeypatch):
    config = await _resolve(
        {
            "destination": "tel:+886223456789",
            "queueHealthUrl": "http://queue:8080/health",
            "queueHealthToken": "t",
        },
        monkeypatch,
    )
    assert config["queueHealthUrl"] == "http://queue:8080/health"
    assert config["queueHealthToken"] == "t"


@pytest.mark.asyncio
async def test_read_path_fails_closed_when_the_parser_is_unmounted(monkeypatch):
    monkeypatch.setenv("PLATFORM_SIP_URI", "/nonexistent/sip_uri.py")
    platform_scope.reset_cache()
    assert await _resolve({"destination": "tel:+886223456789"}, monkeypatch) is None
