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
    fallback scans every ``@``-separated part instead (review M-4), so it is
    strictly more blocking than the pre-W2a user-part-only check, never less.
    """
    from api.services.pipecat.capacity_gate import _premium_rate

    monkeypatch.setenv("PLATFORM_SIP_URI", "/nonexistent/sip_uri.py")
    platform_scope.reset_cache()

    assert _premium_rate("sip:1900@pbx.example") is True
    assert _premium_rate("sip:queue@886204.example") is True  # was the lost coverage
    assert _premium_rate("tel:+886223456789") is False
    assert _premium_rate("sip:queue@pbx.example") is False


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


# ── 6. 通話期第三條 tool 路徑：MCP session（review B-3）────────────────────


def _mcp_tool(name="crm", url="http://mcp.example/mcp"):
    return _tool(
        name,
        ToolCategory.MCP.value,
        {"schema_version": 1, "type": "mcp", "config": {"url": url}},
    )


async def _open_sessions(tools, monkeypatch, *, scope):
    """Drive ``PipecatEngine._open_mcp_sessions`` unbound over a fake engine.

    Returns the list of URLs a session was actually constructed for. The real
    ``McpToolSession`` is swapped out because the point of the test is that
    nothing reaches the network — leaving it in would make a regression here
    an outbound connection from the test suite rather than a red assert.
    """
    from api.db import db_client
    from api.services.workflow import pipecat_engine as engine_mod

    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(scope))
    platform_scope.reset_cache()

    dialled: list[str] = []

    class _FakeSession:
        def __init__(self, **kwargs):
            dialled.append(kwargs["url"])
            self.available = True

        async def start(self):
            return None

    monkeypatch.setattr(engine_mod, "McpToolSession", _FakeSession)
    monkeypatch.setattr(db_client, "get_tools_by_uuids", AsyncMock(return_value=tools))

    node = MagicMock()
    node.tool_uuids = [t.tool_uuid for t in tools]
    engine = MagicMock()
    engine.workflow.nodes = {"1": node}
    engine._get_organization_id = AsyncMock(return_value=42)
    engine._mcp_sessions = {}

    await engine_mod.PipecatEngine._open_mcp_sessions(engine)
    return dialled, engine._mcp_sessions


@pytest.mark.asyncio
async def test_mcp_sessions_are_not_opened_for_a_category_off_the_canon(monkeypatch):
    """B-3: the enabled set has to be consulted **before** the connection.

    This path runs at call start, ahead of any advertisement or registration,
    and it is the one that carries the credential to the URL out of the DB row.
    A filter that only ran downstream would leave the connection already made.
    """
    dialled, sessions = await _open_sessions(
        [_mcp_tool()], monkeypatch, scope=DELIVERED_SCOPE
    )
    assert dialled == [], "MCP is not in the delivered enabled set; nothing may dial"
    assert sessions == {}


@pytest.mark.asyncio
async def test_mcp_sessions_still_open_when_the_canon_allows_mcp(monkeypatch):
    """Negative control. Without this the test above passes on a broken path.

    ``_open_mcp_sessions`` swallows every exception by design, so a filter that
    accidentally rejected everything — or a fixture that never produced a
    valid definition — would read identically to a working deny.
    """
    from api.tests.support.platform_artifacts import PERMISSIVE_SCOPE

    dialled, sessions = await _open_sessions(
        [_mcp_tool()], monkeypatch, scope=PERMISSIVE_SCOPE
    )
    assert dialled == ["http://mcp.example/mcp"]
    assert len(sessions) == 1


@pytest.mark.asyncio
async def test_mcp_sessions_fail_closed_when_the_canon_is_unreadable(monkeypatch):
    """Same direction as registration: canon absent ⇒ open nothing."""
    dialled, sessions = await _open_sessions(
        [_mcp_tool()], monkeypatch, scope=Path("/nonexistent/feature-scope.json")
    )
    assert dialled == []
    assert sessions == {}


# ── 7. 執行層是第四處形狀規則（review B-5）─────────────────────────────────


def test_executor_rejects_a_second_at():
    """B-5：R-E 正本原本宣稱「多個 `@` 三處皆拒」，而實際有**四處**。

    第四處是執行層的 `_DESTINATION_RE`，它的字集含 `@`，於是
    `sip:a@b@evil.com` 通過。W2a 收斂的三處都只看得到**工作流列裡**的目的地；
    env 來源的兩個（`SAFETYNET_FALLBACK_QUEUE`／`CAPACITY_OVERFLOW_TRANSFER_TO`）
    只經過這一處，所以那句話對 env 路徑不成立——而它正是下一步填 `allowed_hosts`
    的放行依據。
    """
    from api.services.pipecat.livekit_transfer_flow import valid_destination

    assert not valid_destination("sip:a@b@evil.com")
    assert not valid_destination("sip:queue@pbx.example@evil.com")
    assert not valid_destination("sip:pbx.example")  # user@host，不是裸 host
    assert not valid_destination("sip:queue@")
    # 一般形不得被這次收緊擋掉。**MUST NOT 在此斷言某一台部署的 `.env` 現值**
    # （2026-08-27 母 repo archive 前 security review S-4）：原本這行寫的是交付態
    # `DOGRAH_TRANSFER_DESTINATION` 的**字面現值**，於是①換一台部署就得改測試，
    # 或反過來為了讓測試綠而遷就 `.env`——設定與 enforcement 被綁在一起；
    # ②該值隨映像與 public fork 出貨（本例是 loopback，無曝險，但形狀是壞的）。
    # 這裡要驗的是**形狀**：user@裸 IP、不帶 port。
    assert valid_destination("sip:queue@127.0.0.1")
    assert valid_destination("sip:queue@pbx.example:5070")
    assert valid_destination("tel:+886223456789")


def test_env_sourced_destinations_are_gated_by_the_executor(monkeypatch):
    """兩個 env 出口的開機閘：形狀由執行層擋，高費率由 `_premium_rate` 擋。

    這兩條是「env 目的地今天唯一的執行期閘」這句話的釘子。第二段同時涵蓋
    M-4：`sip:queue@x@886204.example` 兩個缺陷曾經同時沒關——多個 `@` 過形狀閘，
    而它落進的 legacy fallback 只看第一個 `@` 之前，於是高費率主機也沒擋到。
    """
    from api.services.pipecat.capacity_gate import (
        _premium_rate,
        validate_capacity_config,
    )
    from api.services.pipecat.livekit_safetynet import validate_safetynet_config

    monkeypatch.setenv("SAFETYNET_FALLBACK_QUEUE", "sip:a@b@evil.com")
    with pytest.raises(RuntimeError, match="not tel:"):
        validate_safetynet_config()

    monkeypatch.delenv("SAFETYNET_FALLBACK_QUEUE", raising=False)
    monkeypatch.setenv("CAPACITY_OVERFLOW_TRANSFER_TO", "sip:queue@x@886204.example")
    with pytest.raises(RuntimeError):
        validate_capacity_config()
    assert _premium_rate("sip:queue@x@886204.example") is True, (
        "形狀閘與高費率閘要各自成立——只靠其中一條，另一條放寬時就無聲失守"
    )


# ── 8. Codex review（2026-08-20，PR #15 merge 後）─────────────────────────


@requires_sip_uri
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_url",
    ["http://[bad]/health", "http://[::1/health", "http://[bad]:8080/health"],
)
async def test_unparseable_health_url_drops_only_the_probe_keys(monkeypatch, bad_url):
    """`urlsplit` 自己會對某些輸入拋 ValueError，而拋出去就不是「只掉兩把鑰匙」。

    逃出 `revalidate_transfer_config` 之後：語音 handler 變成泛用
    `execution_error`，容量閘的 `except` 則降級成**空 config**——排程閘與隊列
    健康閘一起靜默關掉。那正是 B-1／M-5 關掉的失效形狀換一扇門進來。
    """
    config = await _resolve(
        {
            "destination": "tel:+886223456789",
            "queueHealthUrl": bad_url,
            "queueHealthToken": "t",
        },
        monkeypatch,
    )
    assert config is not None, "整條 config 不得因為一個壞健康 URL 而消失"
    assert config["destination"] == "tel:+886223456789", "轉接目標必須活著"
    assert "queueHealthUrl" not in config
    assert "queueHealthToken" not in config


def test_health_url_problem_never_raises():
    """上一條的單元層：這個函式對任何字串都只回字串或 None。"""
    from api.services.pipecat.transfer_call_config import _health_url_problem

    for value in (
        "http://[bad]/health",
        "http://[::1/health",
        "://",
        "http://",
        "%%",
        "http://a b/c",
        "",
        "http://queue:8080/health",
    ):
        result = _health_url_problem(value)
        assert result is None or isinstance(result, str), (value, result)


def test_unparseable_health_url_reason_does_not_quote_the_input():
    """理由字串會進通話 log，而 urlsplit 自己的訊息會把原值引出來。"""
    from api.services.pipecat.transfer_call_config import _health_url_problem

    problem = _health_url_problem("http://[secret-internal-host]/health")
    assert problem and "secret-internal-host" not in problem


@pytest.mark.asyncio
async def test_only_a_denied_node_clears_the_schema_not_an_end_node():
    """**H-1 的釘子**：三態各釘一格，而中間那格是這次差點出事的地方。

    第一版的修正把「空清單」一律當成「工具被拒」，並在 docstring 宣稱那只會發生在
    「工具全被拒的終端節點」。實際上 `EndCallNodeData` **連 `tool_uuids` 欄位都沒有**，
    `Node.__init__` 的 `getattr` 因此回 `None`——於是**每個工作流的每個 end node**
    都走那條分支，也就是每一通電話的正常結束路徑。

    那條路徑上 context 還留著轉場函式的 `tool_use`／`tool_result`，而清掉 tools 之後
    仍會發一次 completion。Bedrock 對這個形狀有明文守衛（`has_tool_content and not
    tools` → 塞一個 no-op tool），Anthropic 直接拒。所以「end node 不清」不是潔癖，
    是那條路徑不能碰。
    """
    from unittest.mock import AsyncMock as _AsyncMock

    from pipecat.processors.aggregators.llm_context import NOT_GIVEN

    from api.services.workflow.pipecat_engine import PipecatEngine

    def _engine():
        e = MagicMock()
        e.trust_enforced = False
        e.llm._update_settings = _AsyncMock()
        e.llm._context = object()
        return e

    # ① 有工具 → 設 schema
    from pipecat.adapters.schemas.function_schema import FunctionSchema

    schema = FunctionSchema(
        name="end_call", description="掛斷", properties={}, required=[]
    )
    e1 = _engine()
    await PipecatEngine._update_llm_context(e1, "p", [schema], tools_were_denied=False)
    assert e1.context.set_tools.call_args[0][0].standard_tools == [schema]

    # ② 宣告了工具但全被拒 → 清掉（這是修正存在的理由）
    e2 = _engine()
    await PipecatEngine._update_llm_context(e2, "p", [], tools_were_denied=True)
    e2.context.set_tools.assert_called_once_with(NOT_GIVEN)

    # ③ 本來就沒宣告工具（end node）→ 完全不碰，維持上游行為
    e3 = _engine()
    await PipecatEngine._update_llm_context(e3, "p", [], tools_were_denied=False)
    e3.context.set_tools.assert_not_called()


def test_an_end_node_declares_no_tools_at_all():
    """H-1 的事實基礎，直接對型別釘住而不是對行為推論。

    `tools_were_denied = bool(node.tool_uuids) and not functions` 這條式子的正確性
    完全建立在「end node 的 `tool_uuids` 是 falsy」上。用真的 `EndCallNodeData`
    建一個 `Node` 來釘——上游哪天給 end node 加上 `tool_uuids`，這條會紅，而那時
    `_update_llm_context` 的三態分類要重想。
    """
    from api.services.workflow.dto import EndCallNodeData
    from api.services.workflow.workflow_graph import Node

    node = Node(
        id="end1", node_type="endCall", data=EndCallNodeData(name="bye", prompt="掰掰")
    )
    assert node.is_end is True
    assert not node.tool_uuids, "end node 若開始宣告工具，H-1 的分類就不成立"
    assert not getattr(node, "document_uuids", None)
    assert node.out_edges == []


@pytest.mark.asyncio
async def test_an_end_node_composes_to_empty_and_a_node_with_out_edges_does_not():
    """兩個方向都釘。第一版只釘了後者，所以 H-1 沒被抓到。

    後者（有出邊 → 不可能為空）本來就成立，因為 `compose_functions_for_node` 對每條
    out edge 各加一個轉場 schema。前者才是關鍵：終端節點真的會組出空清單，而它
    **不需要任何工具被拒**就會發生。
    """
    from api.services.workflow.dto import EndCallNodeData
    from api.services.workflow.pipecat_engine_context_composer import (
        compose_functions_for_node,
    )
    from api.services.workflow.workflow_graph import Node

    end_node = Node(
        id="end1", node_type="endCall", data=EndCallNodeData(name="bye", prompt="掰掰")
    )
    assert (
        await compose_functions_for_node(node=end_node, custom_tool_manager=None) == []
    ), "end node 組出空清單——這正是不能一律清 schema 的原因"

    edge = MagicMock()
    edge.get_function_name.return_value = "go_to_next"
    edge.condition = "客戶說完問題"
    node = MagicMock()
    node.document_uuids = []
    node.tool_uuids = ["uuid-calc"]
    node.out_edges = [edge]
    node.mcp_tool_filters = None
    manager = MagicMock()
    manager.get_tool_schemas = AsyncMock(return_value=[])  # 工具全被拒

    functions = await compose_functions_for_node(node=node, custom_tool_manager=manager)
    assert len(functions) == 1, "有出邊就不會是空清單——所以清 schema 只發生在終端節點"


# ── 9. LOW 批次（2026-08-20）────────────────────────────────────────────────


def test_scope_deny_log_cannot_be_forged_with_a_newline():
    """L-1：工具名在 W2c 開寫入面後是受攻擊者影響的，而 R-P 的緩解就是 grep 這行。

    換行不加引號就能偽造第二行 `scope.tool_denied`，把一支被擋的工具混成兩支、
    或反過來讓真正被擋的那支看起來像雜訊。
    """
    from api.services.workflow.pipecat_engine_custom_tools import (
        log_scope_denied_tool,
    )

    messages: list[str] = []

    class _Sink:
        def bind(self, **_kw):
            return self

        def warning(self, message):
            messages.append(message)

    import api.services.workflow.pipecat_engine_custom_tools as mod

    original = mod.logger
    mod.logger = _Sink()
    try:
        log_scope_denied_tool(
            "calculator", "innocent\nscope.tool_denied category=end_call tool=fake"
        )
    finally:
        mod.logger = original

    assert len(messages) == 1
    assert "\n" not in messages[0], "工具名的換行必須被引號逃逸掉，否則可偽造日誌行"


@requires_sip_uri
def test_premium_rate_rejection_adds_no_second_copy_of_the_destination():
    """R-6，**但只能修到一半，而另一半修不掉**。

    finding 說的是：同一個函式上方十行才立下「理由不含原值」的紀律，這裡卻把
    原值印回去。訊息已經改掉了——但 `str(ValidationError)` 仍含該值，因為
    **pydantic 自己會附上 `input_value=...`**，那不在我們的控制範圍內。

    所以這條釘的是能釘的：驗證器自己的訊息不再貢獻**第二份**。順帶記錄那條紀律
    在寫入層本來就比在解析器弱——這裡的值是寫的人剛送進來的，422 也是回給他，
    不構成揭露；`parse_refer_uri` 的 reason 不同，它會進部署 log 而值可能是
    `${ENV:…}` 代換後的真值。詳見 low-findings-register.md。
    """
    from pydantic import ValidationError

    from api.schemas.tool import TransferCallConfig

    secret = "sip:1900555@internal-pbx.corp.example"
    with pytest.raises(ValidationError) as excinfo:
        TransferCallConfig(destination=secret)
    message = str(excinfo.value)
    assert "premium-rate" in message, "拒絕理由本身要留著"
    assert message.count(secret) <= 1, (
        "驗證器的訊息不得再貢獻一份原值——僅剩的那一份是 pydantic 的 input_value="
    )
    assert "Destination 'sip:" not in message, "舊的回顯格式不得復活"


def test_no_second_enabled_set_predicate_survives():
    """L-7／R-5：`tool_category_allowed` 是死碼，而它剛好是 B-3 缺的那個判定。

    兩份判定並存正是本 change 要消滅的形狀——留著它，下次有人改其中一份就漂移。
    """
    assert not hasattr(platform_scope, "tool_category_allowed"), (
        "死碼已於 2026-08-20 移除；要復活它就要讓三個呼叫點都改用它，"
        "不能與 `category not in allowed_categories` 並存"
    )
