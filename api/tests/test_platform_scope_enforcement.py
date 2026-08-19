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

    The destination comes back **blanked, not None** (review B-1 / M-5): callers
    must be able to tell "no transfer tool" from "tool with a rejected
    destination" — only the latter reaches the alert branch W0 added.
    """
    for bad in ("sip:a@b@evil.com", "SIP/human-queue@10.0.0.1"):
        config = await _resolve({"destination": bad}, monkeypatch)
        assert config is not None and config["destination"] == ""


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
    config = await _resolve({"destination": "tel:+886223456789"}, monkeypatch)
    assert config is not None and config["destination"] == "", (
        "fail-closed 是「撥不出去」，不是「這個工作流沒有轉接工具」——後者會連"
        "營運時段閘與隊列健康閘一起靜默關掉（B-1／M-5）"
    )


# ── 5. 不變量：解析器接受集合 ⊆ 執行層接受集合（H-1）────────────────────


@requires_sip_uri
@pytest.mark.parametrize("vector", ACCEPTED, ids=lambda v: v["value"][:40])
def test_parser_is_a_subset_of_the_executor(vector):
    """本檔是唯一同時看得到兩者的地方，所以釘子只能放在這裡。

    `deploy/bin/sip_uri.py` 可以比執行層更嚴（那只是拒絕一個撥得出去的形狀，會被
    使用者當場發現），**不可以更寬**——更寬的每一格都是啞彈：通過部署期形狀閘、
    通過寫入期驗證，REFER 時才失敗，而失敗的是「轉真人」。

    2026-08-19 的 security review（H-1）抓到它一開始正是更寬的：user part 多收十個
    RFC 3261 標點（`! $ & ' ( ) * + = ~`），並額外收 `sips:`。
    """
    from api.services.pipecat.livekit_transfer_flow import valid_destination

    value = vector["value"]
    assert valid_destination(value), (
        f"解析器接受 {value!r} 但執行層 _DESTINATION_RE 不接受——這是一顆啞彈。"
        f"要放寬先放寬執行層，再改 sip_uri.py 與 vectors.json。"
    )


@requires_sip_uri
def test_ai_initiated_transfer_revalidates_its_own_config():
    """M-8：AI 主動觸發的轉接讀的是**這一支工具**的 config，不走 find_transfer_call_config。

    它因此需要自己呼叫 `revalidate_transfer_config`。這條釘的是那個函式對該路徑
    的輸入形狀真的擋得住——handler 本體的接線由下一條測試涵蓋。
    """
    from api.services.pipecat.transfer_call_config import revalidate_transfer_config

    # 被拒的目的地回來是**空字串**不是 None（review B-1／M-5）：None 與「這個工作流
    # 沒有轉接工具」無法區分，而 press-0 的告警分支與容量閘的排程／隊列健康閘都以
    # `if config` 為條件——回 None 會把它們一起靜默關掉，而那正是 W0 修過的失效。
    for bad in ("sip:a@b@evil.com", "SIP/human-queue@10.0.0.1", "tel:+19005551212"):
        out = revalidate_transfer_config({"destination": bad, "schedule": {"tz": "X"}})
        assert out is not None, "回 None 會讓呼叫端誤判成「這個工作流沒有轉接工具」"
        assert out["destination"] == ""
        assert out["schedule"] == {"tz": "X"}, "其餘設定必須留著，閘才不會被一併關掉"
    ok = revalidate_transfer_config({"destination": "tel:+886223456789"})
    assert ok and ok["destination"] == "tel:+886223456789"


@requires_sip_uri
def test_transfer_handler_wiring_calls_the_revalidator():
    """接線本身：handler 必須真的呼叫再驗，而不是只有 find_transfer_call_config 有。

    以原始碼確認呼叫點存在——handler 是深層巢狀閉包，完整驅動它需要 workflow_run、
    transport、LLM 一整套 mock，而那組 mock 本身比被測的一行還脆。這條的價值在於
    「有人把那行刪掉時會紅」。
    """
    import inspect

    from api.services.workflow import pipecat_engine_custom_tools as mod

    src = inspect.getsource(mod)
    assert "revalidate_transfer_config(config or {})" in src
