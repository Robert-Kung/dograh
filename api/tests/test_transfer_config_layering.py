"""部署層／話術層分層的回歸（W3a §1.7–1.10）。

本檔守的是四件事，每一件都是本 change 的一種具體失效形態：

1. **部署層覆蓋，資料庫殘值不得勝出**——分層的整個防護論述就是這一句。
2. **「搬到 env 就不驗了」**——本 change 最主要的失敗形態；env 供給的目的地
   仍須過形狀閘與高費率前綴。
3. **三個讀取入口都吃得到覆蓋**——原設計把 merge 放在
   ``find_transfer_call_config``，那會漏掉最高流量的那一條。
4. **開機期驗證會報**——欄位移出後，「值有沒有被供給」少了兩個執行點。
"""

from __future__ import annotations

import inspect
import types

import pytest

from api.services.pipecat import transfer_call_config as tcc
from api.services.pipecat.transfer_call_config import (
    _DEPLOYMENT_ENV_KEYS,
    deployment_transfer_config,
    revalidate_transfer_config,
    validate_transfer_config,
)
from api.tests.support.platform_artifacts import (
    SUPPORT_DIR,
    requires_sip_uri,
)

HEALTH_URL_SCOPE = SUPPORT_DIR / "feature_scope_health_url.json"

#: 指向一個不存在的正本＝模擬「少掛了一個 `-v`」。用不存在的路徑而不是空檔：
#: 空檔會走「正本裡沒有這條規則」那條（那是版控錯誤，**擋開機**），
#: 而這裡要測的是「檔案根本不在」（那是掛載失誤，**不擋開機**）。
MISSING_SCOPE_PATH = SUPPORT_DIR / "does-not-exist-feature-scope.json"
NO_RULE_SCOPE = SUPPORT_DIR / "feature_scope_no_allowlist.json"

GOOD_DESTINATION = "tel:+886912345678"
DB_DESTINATION = "tel:+886900000001"
GOOD_HEALTH_URL = "http://queue:8080/internal/health"

ENV_BY_KEY = dict(_DEPLOYMENT_ENV_KEYS)


@pytest.fixture(autouse=True)
def _fresh_canon():
    """``platform_scope`` memoizes both artifacts — they are read-only bind
    mounts in production and the call-time filter runs per tool per call.
    Pointing ``PLATFORM_FEATURE_SCOPE`` at a fixture is therefore a no-op
    unless the cache is dropped, and a stale cache here would make the
    allowlist tests pass against whichever canon a previous test loaded."""
    from api.services import platform_scope

    platform_scope.reset_cache()
    yield
    platform_scope.reset_cache()


def _clear_deployment_env(monkeypatch):
    for _key, env_name in _DEPLOYMENT_ENV_KEYS:
        monkeypatch.delenv(env_name, raising=False)


def _set_deployment_env(monkeypatch, **values):
    """``key=value`` in tool-config key names, not env names."""
    _clear_deployment_env(monkeypatch)
    for key, value in values.items():
        monkeypatch.setenv(ENV_BY_KEY[key], value)


def _db_config(**overrides) -> dict:
    """A stored definition.config with the pre-W3a shape: all sixteen keys."""
    config = {
        "destination": DB_DESTINATION,
        "messageType": "custom",
        "customMessage": "轉接中",
        "audioRecordingId": None,
        "timeout": 30,
        "schedule": None,
        "afterHoursAction": "back_to_ai",
        "afterHoursMessage": "非營業時間",
        "alternateDestination": None,
        "transferFailedMessage": "轉接失敗",
        "transferUnavailableMessage": "目前無法接聽",
        "unavailableAnnounceLimit": 2,
        "queueHealthUrl": "http://queue:8080/db-residue",
        "queueHealthToken": "db-residue-token",
        "queueHealthTimeoutSeconds": 2.0,
        "queueHealthCacheTtlSeconds": 5.0,
    }
    config.update(overrides)
    return config


# ── 1.7 覆蓋方向 ────────────────────────────────────────────────────────


def test_deployment_reader_reads_environ_every_call(monkeypatch):
    """D2：MUST NOT 在 import 期讀成模組常數。

    憑證輪替的宣稱（「改 env 就生效、不需要 re-apply」）整個掛在這一條上：
    import 期讀的話輪替需要重啟 dograh-api，那會斷掉進行中的通話。
    """
    _set_deployment_env(monkeypatch, queueHealthToken="first")
    assert deployment_transfer_config()["queueHealthToken"] == "first"
    monkeypatch.setenv(ENV_BY_KEY["queueHealthToken"], "rotated")
    assert deployment_transfer_config()["queueHealthToken"] == "rotated"


def test_blank_env_is_unset_not_empty_string(monkeypatch):
    """``.env`` 裡一個沒填值的鍵是「沒設定」，不是「設定成空字串」。

    設成空字串會讓一個空的 ``QUEUE_HEALTH_URL`` 覆蓋掉資料庫值 → 探測整個關掉
    （``queue_is_healthy`` 的 ``if not url: return True``），而操作者看到的
    ``.env`` 只是一行沒填。
    """
    _set_deployment_env(monkeypatch, queueHealthUrl="   ")
    assert "queueHealthUrl" not in deployment_transfer_config()


@requires_sip_uri
def test_env_overrides_db_value(monkeypatch):
    _set_deployment_env(
        monkeypatch,
        destination=GOOD_DESTINATION,
        queueHealthUrl=GOOD_HEALTH_URL,
        queueHealthToken="env-token",
    )
    merged = revalidate_transfer_config(_db_config())
    assert merged["destination"] == GOOD_DESTINATION
    assert merged["queueHealthUrl"] == GOOD_HEALTH_URL
    assert merged["queueHealthToken"] == "env-token"


@requires_sip_uri
def test_db_residue_must_not_win(monkeypatch):
    """殘留（或經寫入路徑塞入）的憑證與目的地 MUST NOT 生效。

    這一條與上一條不是同一件事：上一條問「有沒有讀到 env」，這一條問
    「資料庫那份還在不在生效值裡」。原設計的 merge 點會讓最高流量的那條路徑
    在這一題上答錯。
    """
    _set_deployment_env(
        monkeypatch,
        destination=GOOD_DESTINATION,
        queueHealthUrl=GOOD_HEALTH_URL,
        queueHealthToken="env-token",
    )
    merged = revalidate_transfer_config(
        _db_config(
            destination="sip:attacker@evil.example",
            queueHealthUrl="http://queue:8080/db-residue",
            queueHealthToken="db-residue-token",
        )
    )
    assert DB_DESTINATION not in merged.values()
    assert merged["destination"] == GOOD_DESTINATION
    assert merged["queueHealthToken"] == "env-token"
    assert "db-residue" not in merged["queueHealthUrl"]


@requires_sip_uri
def test_missing_env_leaves_the_key_absent_and_never_falls_back_to_db(monkeypatch):
    """§5.1：限期 fallback 已移除。缺值 → **該鍵不存在**，MUST NOT 用資料庫值。

    資料庫裡那個值可能是分層之前的舊憑證，也可能是經某條寫入路徑塞進來的目的地。
    「部署層覆蓋一切」是分層的整個防護論述——退回讀它就是把論述取消掉。
    """
    _clear_deployment_env(monkeypatch)

    # merge 這一層：六個鍵**一個都不留**。
    merged = tcc._merge_deployment_layer(_db_config())
    for key, _env in _DEPLOYMENT_ENV_KEYS:
        assert key not in merged, f"{key} 在 env 缺值時仍留著資料庫的值"

    # revalidate 這一層：它會把 `destination` 補成 `""`（那是 C4 的
    # no_destination 分支，`valid_destination("")` 為 False，不會撥出去）。
    # **重點是它不是資料庫那個值**——「鍵不見了」與「鍵被抹白」對來電者是同一件事，
    # 「鍵還帶著上一版的憑證」才是要防的那件事。
    effective = revalidate_transfer_config(_db_config())
    assert effective["destination"] == ""
    assert DB_DESTINATION not in effective.values()
    assert "db-residue-token" not in effective.values()
    for key, _env in _DEPLOYMENT_ENV_KEYS:
        if key == "destination":
            continue
        assert key not in effective, f"{key} 在 env 缺值時仍留著資料庫的值"


@requires_sip_uri
def test_partially_supplied_env_does_not_resurrect_the_other_keys(monkeypatch):
    """只供給一個鍵時，**其餘五個仍然不得退回資料庫**。

    這是移除 fallback 之後最容易漏掉的形狀：整組缺值會被開機期驗證擋下，
    而「五個有值、一個沒有」開得起來（那一個若是選填的 alternateDestination），
    於是只有它會靜默拿到殘值。
    """
    _clear_deployment_env(monkeypatch)
    monkeypatch.setenv("DOGRAH_TRANSFER_DESTINATION", GOOD_DESTINATION)
    merged = tcc._merge_deployment_layer(_db_config())
    assert merged["destination"] == GOOD_DESTINATION
    for key, _env in _DEPLOYMENT_ENV_KEYS:
        if key == "destination":
            continue
        assert key not in merged, f"{key} 未供給卻拿到了資料庫的值"


def test_the_transitional_flags_are_gone(monkeypatch):
    """兩個過渡態常數 SHALL 不再存在（不是設成 False——那是死碼）。"""
    assert not hasattr(tcc, "_MIGRATION_DB_FALLBACK")
    assert not hasattr(tcc, "_VALIDATE_BLOCKS_BOOT")


def test_speech_layer_keys_are_never_touched(monkeypatch):
    """merge 只碰六欄。話術層被 merge 動到就是 seed-once 的反面。"""
    _set_deployment_env(monkeypatch, destination=GOOD_DESTINATION)
    before = _db_config()
    merged = tcc._merge_deployment_layer(before)
    speech = set(before) - {key for key, _env in _DEPLOYMENT_ENV_KEYS}
    for key in speech:
        assert merged[key] == before[key]
    assert before["destination"] == DB_DESTINATION, "merge mutated its input"


# ── 1.8 「搬到 env 就不驗了」——本 change 最主要的失敗形態 ──────────────


@requires_sip_uri
def test_env_destination_still_passes_the_shape_gate(monkeypatch):
    _set_deployment_env(monkeypatch, destination="sip:a@b@evil.example")
    merged = revalidate_transfer_config(_db_config())
    assert merged["destination"] == "", "a malformed env destination was dialled"


#: Well-formed E.164 that also matches a premium-rate prefix. It has to be
#: **shape-valid**, otherwise the premium tests pass for the wrong reason: the
#: shape gate blanks the destination first and the premium guard never runs.
PREMIUM_DESTINATION = "tel:+886204123456"


@requires_sip_uri
def test_premium_fixture_is_shape_valid():
    """守著上面那個 fixture 的前提。

    ``PREMIUM_DESTINATION`` 若哪天不再是合法 E.164，兩條高費率測試會**照樣綠**
    ——它們斷言的「被抹白」對形狀失敗也成立。這一條讓那種假綠當場失敗。
    """
    from api.services.platform_scope import parse_refer_uri

    assert parse_refer_uri(PREMIUM_DESTINATION).ok


@requires_sip_uri
def test_env_destination_still_hits_the_premium_rate_guard(monkeypatch):
    _set_deployment_env(monkeypatch, destination=PREMIUM_DESTINATION)
    merged = revalidate_transfer_config(_db_config())
    assert merged["destination"] == "", "a premium-rate env destination was dialled"


@requires_sip_uri
def test_env_alternate_destination_still_validated(monkeypatch):
    _set_deployment_env(
        monkeypatch,
        destination=GOOD_DESTINATION,
        alternateDestination="not-a-refer-target",
    )
    merged = revalidate_transfer_config(_db_config())
    assert "alternateDestination" not in merged


@requires_sip_uri
def test_env_health_url_still_shape_checked(monkeypatch):
    _set_deployment_env(
        monkeypatch,
        destination=GOOD_DESTINATION,
        queueHealthUrl="http://user:pw@queue:8080/health",
        queueHealthToken="env-token",
    )
    merged = revalidate_transfer_config(_db_config())
    assert "queueHealthUrl" not in merged
    assert "queueHealthToken" not in merged, (
        "the token must be dropped with the URL: a probe with no URL sends no "
        "Authorization header, but leaving the key invites a partial config"
    )


# ── 1.9 三個讀取入口 ────────────────────────────────────────────────────
#
# 兩條經 ``find_transfer_call_config``，第三條直接讀 ORM row。行為面在下面各測
# 一次；**結構面另外釘住**，因為本 change 的原始缺陷不是「某條路徑算錯」，而是
# 「有一條路徑沒被算進去」——那種缺陷只有結構斷言抓得到。


def _fake_workflow(tool_uuid: str = "t-1"):
    node = types.SimpleNamespace(tool_uuids=[tool_uuid])
    return types.SimpleNamespace(nodes={"n1": node})


def _fake_tool(config: dict, tool_uuid: str = "t-1"):
    from api.enums import ToolCategory

    return types.SimpleNamespace(
        tool_uuid=tool_uuid,
        category=ToolCategory.TRANSFER_CALL.value,
        definition={"type": "transfer_call", "config": config},
    )


@requires_sip_uri
@pytest.mark.asyncio
async def test_entrypoint_lookup_path_gets_the_override(monkeypatch):
    """入口①②：``capacity_gate`` 與 ``pipecat_engine`` 的共用查詢函式。"""
    _set_deployment_env(
        monkeypatch,
        destination=GOOD_DESTINATION,
        queueHealthUrl=GOOD_HEALTH_URL,
        queueHealthToken="env-token",
    )

    async def fake_get_tools_by_uuids(uuids, organization_id):
        return [_fake_tool(_db_config())]

    monkeypatch.setattr(
        tcc.db_client, "get_tools_by_uuids", fake_get_tools_by_uuids, raising=True
    )
    config = await tcc.find_transfer_call_config(_fake_workflow(), organization_id=1)
    assert config is not None
    assert config["destination"] == GOOD_DESTINATION
    assert config["queueHealthToken"] == "env-token"
    # 形狀對呼叫端不變：話術層鍵原封不動，呼叫端不需要感知分層。
    assert config["transferFailedMessage"] == "轉接失敗"


@requires_sip_uri
def test_entrypoint_orm_path_gets_the_override(monkeypatch):
    """入口③：``pipecat_engine_custom_tools.transfer_call_handler``。

    它讀 ``tool.definition["config"]`` 之後自行呼叫 ``revalidate_transfer_config``
    ——本測試複製那個呼叫形狀。這是自陳的 "the highest-volume trigger"，
    也正是原設計會漏掉的那一條。
    """
    _set_deployment_env(
        monkeypatch,
        destination=GOOD_DESTINATION,
        queueHealthUrl=GOOD_HEALTH_URL,
        queueHealthToken="env-token",
    )
    tool = _fake_tool(_db_config())
    config = revalidate_transfer_config(tool.definition.get("config", {}) or {})
    assert config["destination"] == GOOD_DESTINATION
    assert config["queueHealthToken"] == "env-token"
    assert config["queueHealthUrl"] == GOOD_HEALTH_URL


def test_every_reader_reaches_the_convergence_point():
    """結構釘：三個讀取入口都收斂到 ``revalidate_transfer_config``。

    行為測試證明「這三條今天算對了」；本測試證明「沒有第四條偷偷繞過」。
    新增一個讀取點而不經收斂點，就是本 change 修掉的那個缺陷再來一次，
    而它在行為測試上是完全隱形的。
    """
    from api.services.pipecat import capacity_gate
    from api.services.workflow import pipecat_engine, pipecat_engine_custom_tools

    for module in (capacity_gate, pipecat_engine):
        source = inspect.getsource(module)
        assert "find_transfer_call_config" in source, (
            f"{module.__name__} no longer reaches the transfer config through the "
            "shared lookup; if it now reads the ORM row directly it must call "
            "revalidate_transfer_config itself"
        )

    source = inspect.getsource(pipecat_engine_custom_tools)
    assert "revalidate_transfer_config" in source, (
        "the AI-initiated transfer handler stopped calling the convergence "
        "point; the deployment layer is no longer merged on the highest-volume "
        "path and the database value wins there"
    )


# ── 1.10 開機期驗證（警告模式）──────────────────────────────────────────


def _boot_problems() -> list[str]:
    """跑一次 validate_transfer_config，回傳它報出的問題字串。

    §5.2 之後**不合格即 RuntimeError**，所以 problem 自例外訊息讀；同時仍收
    ERROR log——「檢查跑不成」（缺 bind mount）那一類刻意不擋開機，只留在 log 裡。
    兩邊都收，測試才驗得到「拋的是不合格、log 的是沒驗成」這個分工。
    用 loguru 的 sink 而不是 caplog：本 repo 用 loguru，caplog 抓不到。
    """
    captured: list[str] = []
    handler_id = tcc.logger.add(
        lambda message: captured.append(str(message)), level="ERROR"
    )
    try:
        validate_transfer_config()
    except RuntimeError as exc:
        captured.append(str(exc))
    finally:
        tcc.logger.remove(handler_id)
    return captured


def test_boot_validation_reports_missing_values(monkeypatch):
    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(HEALTH_URL_SCOPE))
    _clear_deployment_env(monkeypatch)
    problems = _boot_problems()
    joined = "\n".join(problems)
    for env_name in (
        "DOGRAH_TRANSFER_DESTINATION",
        "QUEUE_HEALTH_URL",
        "QUEUE_HEALTH_TOKEN",
    ):
        assert env_name in joined, f"{env_name} missing was not reported"
    assert "DOGRAH_TRANSFER_ALTERNATE_DESTINATION" not in joined, (
        "the after-hours alternate is optional: not configuring it is a valid "
        "deployment shape, not a defect"
    )


def test_boot_validation_blocks_boot_when_values_are_missing(monkeypatch):
    """§5.2：缺值即擋開機，且**逐項指名**哪個 env 沒設。

    安靜的那一端是 `queue_is_healthy` 對缺席 URL `return True`——健康閘整個消失，
    每位要求真人的來電者被 REFER 進一個可能已死的隊列。拒絕啟動是大聲的那一端。
    """
    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(HEALTH_URL_SCOPE))
    _clear_deployment_env(monkeypatch)
    with pytest.raises(RuntimeError) as excinfo:
        validate_transfer_config()
    message = str(excinfo.value)
    for env_name in (
        "DOGRAH_TRANSFER_DESTINATION",
        "QUEUE_HEALTH_URL",
        "QUEUE_HEALTH_TOKEN",
    ):
        assert env_name in message, f"{env_name} 缺值未被指名"


def test_boot_validation_never_silently_falls_back_to_db(monkeypatch):
    """§5.3 逐字：MUST NOT 靜默退回讀 DB。

    兩件事一起驗才算數——開機**擋下**，而且 merge 出來的結果裡**沒有**資料庫的值。
    只驗其中一件都留得下一條路：擋開機但仍 fallback（開不起來時沒人看得到 merge），
    或不 fallback 但不擋（靜默無值）。
    """
    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(HEALTH_URL_SCOPE))
    _clear_deployment_env(monkeypatch)
    with pytest.raises(RuntimeError):
        validate_transfer_config()
    merged = tcc._merge_deployment_layer(_db_config())
    assert DB_DESTINATION not in merged.values()
    assert "db-residue-token" not in merged.values()


def test_a_fully_supplied_deployment_boots(monkeypatch):
    """收緊之後正常部署仍要起得來——否則這條收緊就是一次全面停機。"""
    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(HEALTH_URL_SCOPE))
    _set_deployment_env(
        monkeypatch,
        destination=GOOD_DESTINATION,
        queueHealthUrl=GOOD_HEALTH_URL,
        queueHealthToken="env-token",
    )
    validate_transfer_config()  # 不得拋


def test_a_missing_bind_mount_does_not_block_boot(monkeypatch):
    """D-A5：一個少掉的 `-v` MUST NOT 變成「平台停止接聽電話」。

    但它 SHALL 大聲說**這個值沒有被驗過**——「沒驗成」被靜默吞掉才是本 change
    一路在防的形狀。
    """
    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(MISSING_SCOPE_PATH))
    _set_deployment_env(
        monkeypatch,
        destination=GOOD_DESTINATION,
        queueHealthUrl=GOOD_HEALTH_URL,
        queueHealthToken="env-token",
    )
    problems = _boot_problems()  # 不得拋 RuntimeError
    joined = "\n".join(problems)
    assert "deploy_config_unverified" in joined
    assert "NOT checked at boot" in joined


@requires_sip_uri
def test_boot_validation_reports_bad_destination_shape(monkeypatch):
    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(HEALTH_URL_SCOPE))
    _set_deployment_env(
        monkeypatch,
        destination="sip:a@b@evil.example",
        queueHealthUrl=GOOD_HEALTH_URL,
        queueHealthToken="env-token",
    )
    joined = "\n".join(_boot_problems())
    assert "DOGRAH_TRANSFER_DESTINATION" in joined
    assert "not a valid REFER target" in joined


@requires_sip_uri
def test_boot_validation_reports_premium_rate(monkeypatch):
    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(HEALTH_URL_SCOPE))
    _set_deployment_env(
        monkeypatch,
        destination=PREMIUM_DESTINATION,
        queueHealthUrl=GOOD_HEALTH_URL,
        queueHealthToken="env-token",
    )
    joined = "\n".join(_boot_problems())
    assert "premium-rate" in joined
    assert "not a valid REFER target" not in joined, (
        "the shape gate fired instead of the premium guard — this test would "
        "then pass without exercising what it names"
    )


def test_boot_validation_reports_host_outside_the_allowlist(monkeypatch):
    """本條是 D11 的核心：白名單是全系統唯一實際生效的 egress 目的地清單。

    欄位移出 ``definition.config`` 之後 ``check_definition`` 再也命中不到它；
    這裡與 preflight 是它剩下的兩個執行點。
    """
    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(HEALTH_URL_SCOPE))
    _set_deployment_env(
        monkeypatch,
        destination=GOOD_DESTINATION,
        queueHealthUrl="http://169.254.169.254/latest/meta-data/",
        queueHealthToken="env-token",
    )
    joined = "\n".join(_boot_problems())
    assert "QUEUE_HEALTH_URL host" in joined
    assert "queue:8080" in joined


def test_boot_validation_reports_scheme_outside_the_allowlist(monkeypatch):
    """2.14a：正本只允許 ``http``，通話期的 ``_health_url_problem`` 放行 https。

    分層前由部署期的嚴格側收斂；欄位移出後若只剩寬鬆側即為實質放寬，
    故開機期這一關讀的是正本而不是 ``_HEALTH_URL_SCHEMES``。
    """
    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(HEALTH_URL_SCOPE))
    _set_deployment_env(
        monkeypatch,
        destination=GOOD_DESTINATION,
        queueHealthUrl="https://queue:8080/internal/health",
        queueHealthToken="env-token",
    )
    joined = "\n".join(_boot_problems())
    assert "scheme" in joined


def test_boot_validation_reports_probe_seconds_below_floor(monkeypatch):
    """M-6：上游只 clamp 上界，``0.001`` 會被誠實採用 → 真人轉接全滅。"""
    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(HEALTH_URL_SCOPE))
    _set_deployment_env(
        monkeypatch,
        destination=GOOD_DESTINATION,
        queueHealthUrl=GOOD_HEALTH_URL,
        queueHealthToken="env-token",
        queueHealthTimeoutSeconds="0.001",
    )
    joined = "\n".join(_boot_problems())
    assert "QUEUE_HEALTH_TIMEOUT_SECONDS" in joined
    assert "floor" in joined


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-inf", "1e400"])
def test_boot_validation_rejects_non_finite_seconds(monkeypatch, raw):
    """F-3：``nan`` 對每個比較運算子都回 False，所以它從下界底下穿過去。

    這一組是 :func:`test_boot_validation_reports_probe_seconds_below_floor` 的
    對照組——那條證明「太小會被擋」，而在補上 ``math.isfinite`` 之前，**比太小更糟的
    值反而通過**：``nan`` 生效後 ``asyncio.wait_for(timeout=nan)`` 立即逾時，
    也就是那個下界存在的理由以最徹底的形式發生。``1e400`` 收在這裡是因為
    ``float()`` 把它變成 ``inf`` 而不是拋錯。
    """
    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(HEALTH_URL_SCOPE))
    _set_deployment_env(
        monkeypatch,
        destination=GOOD_DESTINATION,
        queueHealthUrl=GOOD_HEALTH_URL,
        queueHealthToken="env-token",
        queueHealthTimeoutSeconds=raw,
    )
    joined = "\n".join(_boot_problems())
    assert "QUEUE_HEALTH_TIMEOUT_SECONDS" in joined
    assert "not a finite number" in joined


def test_a_finite_in_range_value_still_boots(monkeypatch):
    """非有限值那條擋門 MUST NOT 連正常值一起擋——沒有這條，`return` 掉整個迴圈也會綠。"""
    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(HEALTH_URL_SCOPE))
    _set_deployment_env(
        monkeypatch,
        destination=GOOD_DESTINATION,
        queueHealthUrl=GOOD_HEALTH_URL,
        queueHealthToken="env-token",
        queueHealthTimeoutSeconds="1.5",
    )
    assert _boot_problems() == []


def test_boot_validation_reports_non_numeric_seconds(monkeypatch):
    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(HEALTH_URL_SCOPE))
    _set_deployment_env(
        monkeypatch,
        destination=GOOD_DESTINATION,
        queueHealthUrl=GOOD_HEALTH_URL,
        queueHealthToken="env-token",
        queueHealthCacheTtlSeconds="soon",
    )
    joined = "\n".join(_boot_problems())
    assert "QUEUE_HEALTH_CACHE_TTL_SECONDS is not a number" in joined


def test_boot_validation_says_so_when_the_canon_carries_no_rule(monkeypatch):
    """「規則還在正本裡」MUST NOT 被讀成「該控制仍生效」。

    正本若沒有這條規則，本檢查 SHALL 明說沒有可比對的白名單，而不是靜默通過
    ——靜默通過正是本 change 對 preflight §7「空轉全綠」提出的同一個指控。
    """
    from api.tests.support.platform_artifacts import DELIVERED_SCOPE

    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(DELIVERED_SCOPE))
    _set_deployment_env(
        monkeypatch,
        destination=GOOD_DESTINATION,
        queueHealthUrl=GOOD_HEALTH_URL,
        queueHealthToken="env-token",
    )
    joined = "\n".join(_boot_problems())
    assert "no allowlist to check against" in joined


# ── 通話期的白名單執行點（platform review gate F-1／H3）────────────────────
@requires_sip_uri
def test_call_time_allowlist_drops_url_and_token(monkeypatch):
    """白名單在**通話期**也要有執行點，而且不合格時 token SHALL NOT 被送出。

    在此之前它只有兩個執行點：preflight（部署期一次性、有已知繞道）與開機期，
    而開機期在正本讀不到時降級為 ``unverifiable`` **不擋開機**（D-A5 的取捨，
    本身是對的）。於是「掛載存在但檔案讀不動／半寫入／編碼壞掉」的狀態下，
    一個被改壞的 URL 會讓每一次探測把 bearer token 送去該主機。
    """
    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(HEALTH_URL_SCOPE))
    _set_deployment_env(
        monkeypatch,
        destination=GOOD_DESTINATION,
        queueHealthUrl="http://attacker.test/health",
        queueHealthToken="env-token",
    )
    merged = revalidate_transfer_config(_db_config())
    assert "queueHealthUrl" not in merged
    assert "queueHealthToken" not in merged, "憑證仍在——會被送去未通過白名單的主機"
    # C4：探測停用不是死寂，目的地仍在（轉真人仍走得通，只是沒有健康閘）
    assert merged["destination"] == GOOD_DESTINATION


@requires_sip_uri
def test_call_time_unverifiable_canon_also_drops_the_token(monkeypatch):
    """**「檢查跑不成」在通話期與「值不合格」同一處置。**

    這與開機期相反，而理由正是那個不對稱：開機期擋下去等於平台停止接聽電話
    （不可接受）；通話期只是停用探測，而 ``queue_health`` 已明文把「未設定」
    當成可接受降級。代價是失去健康閘（C4 的其他出口兜著），
    收益是憑證不外送到未驗證的主機（沒有其他東西兜）。
    """
    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(MISSING_SCOPE_PATH))
    _set_deployment_env(
        monkeypatch,
        destination=GOOD_DESTINATION,
        queueHealthUrl=GOOD_HEALTH_URL,
        queueHealthToken="env-token",
    )
    merged = revalidate_transfer_config(_db_config())
    assert "queueHealthUrl" not in merged
    assert "queueHealthToken" not in merged
    assert merged["destination"] == GOOD_DESTINATION


@requires_sip_uri
def test_call_time_allowlist_passes_a_good_url(monkeypatch):
    """對照組：合格的值不受影響。沒有這條，一個「一律 pop」的實作也會通過上面兩條。"""
    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(HEALTH_URL_SCOPE))
    _set_deployment_env(
        monkeypatch,
        destination=GOOD_DESTINATION,
        queueHealthUrl=GOOD_HEALTH_URL,
        queueHealthToken="env-token",
    )
    merged = revalidate_transfer_config(_db_config())
    assert merged["queueHealthUrl"] == GOOD_HEALTH_URL
    assert merged["queueHealthToken"] == "env-token"


@requires_sip_uri
def test_a_canon_without_an_allowlist_does_not_disable_the_probe(monkeypatch):
    """正本**刻意**留空 allowlist 是合法狀態（CS-19／R-E 對 destination 就是空的）。

    開機期把它記為 verdict 是對的（「規則還在正本裡」不等於「控制生效」，
    而那是版控裡有人改得掉的錯誤）；通話期照 verdict 處置卻會把健康閘關掉，
    而「沒有規則要執行」不等於「這個值可疑」。兩期的處置刻意不同。
    """
    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(NO_RULE_SCOPE))
    _set_deployment_env(
        monkeypatch,
        destination=GOOD_DESTINATION,
        queueHealthUrl=GOOD_HEALTH_URL,
        queueHealthToken="env-token",
    )
    merged = revalidate_transfer_config(_db_config())
    assert merged["queueHealthUrl"] == GOOD_HEALTH_URL
    assert merged["queueHealthToken"] == "env-token"


# ── 大小寫與預設埠（platform review gate F-7）──────────────────────────────
@pytest.mark.parametrize(
    "url",
    [
        "http://QUEUE:8080/internal/health",
        "http://Queue:8080/internal/health",
    ],
)
@requires_sip_uri
def test_host_matching_is_case_insensitive_like_the_canon(monkeypatch, url):
    """正本 ``_check_url`` 比對的是 casefold 後、補預設埠的 ``hostname:port``。

    這裡原本比的是 raw ``netloc``（``urlsplit`` 不小寫化它），於是
    ``http://QUEUE:8080/…`` **通過 preflight** 卻**擋下 dograh 開機**——
    部署檢查全綠之後平台拒絕接聽電話，訊息還讀起來像真的白名單違規。
    """
    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(HEALTH_URL_SCOPE))
    _set_deployment_env(
        monkeypatch,
        destination=GOOD_DESTINATION,
        queueHealthUrl=url,
        queueHealthToken="env-token",
    )
    assert _boot_problems() == []


# ── 高費率判定不依賴解析器（platform review gate F-6）──────────────────────
def test_premium_rate_is_reported_even_without_the_uri_parser(monkeypatch):
    """``sip_uri.py`` 漏掛時，高費率目的地 SHALL 仍然被報出來。

    ``try`` 原本包住整個迴圈，第一個目的地拋 ``PlatformArtifactMissing`` 就讓它
    整個中止——連 ``_premium_rate`` 都沒跑，而那個檢查刻意做了不需解析器的兜底。
    結果是漏掛時 ``tel:+19005551212`` 開機全綠。通話期有兜底不會真的撥出去，
    但**運維據以行動的開機報告是錯的**，而它同時代表所有轉接都失效。
    """
    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(MISSING_SCOPE_PATH))
    _set_deployment_env(
        monkeypatch,
        destination="tel:+19005551212",
        queueHealthUrl=GOOD_HEALTH_URL,
        queueHealthToken="env-token",
    )
    joined = "\n".join(_boot_problems())
    assert "premium-rate" in joined, joined
    assert "DOGRAH_TRANSFER_DESTINATION" in joined


# ── TTL 0 是被支援的輸入（platform review gate M2）─────────────────────────
def test_cache_ttl_zero_is_honored_not_blocked(monkeypatch):
    """``TTL 0 disables the cache`` 是上游 ``_bounded_seconds`` 的明文契約。

    下界防的是「探測預算小到必然逾時」，而 TTL 不是探測預算——設 0 只是每次都
    重新探測。兩個秒數一視同仁套 0.2 的下界會讓一個**被支援的輸入**擋開機，
    訊息還說「這麼小的探測預算必然逾時」，對 TTL 文不對題。
    """
    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(HEALTH_URL_SCOPE))
    _set_deployment_env(
        monkeypatch,
        destination=GOOD_DESTINATION,
        queueHealthUrl=GOOD_HEALTH_URL,
        queueHealthToken="env-token",
        queueHealthCacheTtlSeconds="0",
    )
    assert _boot_problems() == []


@pytest.mark.parametrize(
    "key,env_name",
    [
        ("queueHealthTimeoutSeconds", "QUEUE_HEALTH_TIMEOUT_SECONDS"),
        ("queueHealthCacheTtlSeconds", "QUEUE_HEALTH_CACHE_TTL_SECONDS"),
    ],
)
def test_negative_seconds_are_rejected_for_both_fields(monkeypatch, key, env_name):
    """負值對兩個欄位都擋：上游對負值**無聲退回預設**，設定值與生效值就此分岔。"""
    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(HEALTH_URL_SCOPE))
    _set_deployment_env(
        monkeypatch,
        destination=GOOD_DESTINATION,
        queueHealthUrl=GOOD_HEALTH_URL,
        queueHealthToken="env-token",
        **{key: "-1"},
    )
    joined = "\n".join(_boot_problems())
    assert env_name in joined and "negative" in joined, joined


def test_the_probe_budget_floor_still_applies(monkeypatch):
    """對照組：把下界縮到只剩 timeout 之後，timeout 的下界仍然要擋。"""
    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(HEALTH_URL_SCOPE))
    _set_deployment_env(
        monkeypatch,
        destination=GOOD_DESTINATION,
        queueHealthUrl=GOOD_HEALTH_URL,
        queueHealthToken="env-token",
        queueHealthTimeoutSeconds="0.001",
    )
    joined = "\n".join(_boot_problems())
    assert "QUEUE_HEALTH_TIMEOUT_SECONDS" in joined and "floor" in joined


# ── fail-open 分支要進告警通道，不是只進容器日誌（platform review gate F-12）──
def test_config_events_reach_the_alert_dispatcher(monkeypatch):
    """``logger.bind(call_event=...)`` 是事件的**形狀**，不是它的**路徑**。

    告警只走 ``alerts.notify()``，而本模組從來沒有呼叫過它——於是 docstring
    承諾的「大聲失敗」只對正在 grep 容器日誌的人成立。這一點是承重的：
    ``deploy_config_unverified`` 是「正本讀不到時不擋開機」這個取捨的**全部**
    補償控制，沒有人被叫醒就等於沒有補償。
    """
    from api.services.observability import alerts

    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(alerts, "notify", lambda e, f: seen.append((e, f)))
    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(MISSING_SCOPE_PATH))
    _set_deployment_env(
        monkeypatch,
        destination=GOOD_DESTINATION,
        queueHealthUrl=GOOD_HEALTH_URL,
        queueHealthToken="env-token",
    )
    _boot_problems()
    assert any(e == "transfer.deploy_config_unverified" for e, _ in seen), seen


def test_the_config_event_names_are_actually_routable():
    """事件名要真的落在 immediate／windowed 其中一邊。

    ``notify`` 對兩個集合以外的名字**直接 return**——名字打錯、或加了事件卻忘了
    登記，症狀都是「送不出去」而且完全無聲。這條把登記本身變成斷言。
    """
    from api.services.observability import alerts

    for event in (
        "transfer.deploy_config_unverified",
        "transfer.config_unvalidatable",
        "transfer.config_rejected",
    ):
        assert event in (alerts.IMMEDIATE_EVENTS | alerts.WINDOWED_EVENTS), event
    # 每通都會重報的那一個 SHALL 是 windowed，否則壞設定會在最需要讀告警的時候洗版
    assert "transfer.config_rejected" in alerts.WINDOWED_EVENTS


def test_alert_dispatch_failure_never_breaks_call_handling(monkeypatch):
    """C4：告警通道壞掉 MUST NOT 影響通話處理。"""
    from api.services.observability import alerts

    def boom(*a, **kw):
        raise RuntimeError("webhook exploded")

    monkeypatch.setattr(alerts, "notify", boom)
    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(HEALTH_URL_SCOPE))
    _set_deployment_env(
        monkeypatch,
        destination=GOOD_DESTINATION,
        queueHealthUrl="http://attacker.test/health",
        queueHealthToken="env-token",
    )
    merged = revalidate_transfer_config(_db_config())
    assert "queueHealthToken" not in merged
    assert merged["destination"] == GOOD_DESTINATION


# ── 節點圖掉了工具引用（platform review gate F-11）────────────────────────
class _FakeNode:
    def __init__(self, tool_uuids=None):
        self.tool_uuids = tool_uuids or []


class _FakeWorkflow:
    def __init__(self, nodes):
        self.nodes = {str(i): n for i, n in enumerate(nodes)}


@pytest.mark.asyncio
@requires_sip_uri
async def test_a_node_graph_without_tool_uuids_still_gets_the_deployment_layer(
    monkeypatch,
):
    """移除 `tool_uuids` 是 admission **會放行**的一次寫入，而它從下游繞開分層。

    早退在 merge **之前**，於是 `capacity_gate` 走 `config or {}` →
    `queue_is_healthy({})` 的 fail-open（滿線溢流 REFER 進可能已死的隊列，排程閘
    同時失效），`press0_gate` 走安靜分支（`transfer.failed` 的守衛是
    `if transfer_config and ...`）。開機期與 preflight 都是綠的——它們驗的是 env，
    而這條路徑根本走不到讀 env 的那個函式。
    """
    monkeypatch.setenv("PLATFORM_FEATURE_SCOPE", str(HEALTH_URL_SCOPE))
    _set_deployment_env(
        monkeypatch,
        destination=GOOD_DESTINATION,
        queueHealthUrl=GOOD_HEALTH_URL,
        queueHealthToken="env-token",
    )
    config = await tcc.find_transfer_call_config(_FakeWorkflow([_FakeNode()]), 1)
    assert config is not None, "節點圖掉了引用就整條轉真人路徑消失"
    assert config["destination"] == GOOD_DESTINATION
    assert config["queueHealthUrl"] == GOOD_HEALTH_URL, "健康閘 SHALL NOT fail-open"


@pytest.mark.asyncio
async def test_a_workflow_that_never_transfers_still_returns_none(monkeypatch):
    """對照組：部署層沒有宣告目的地時 `None` 仍然正確。

    「這個工作流本來就不轉真人」是合法選擇，press-0 的安靜分支正是為它存在的。
    沒有這條，一個「一律回傳設定」的實作會把每個工作流都變成有 press-0 的。
    """
    for name in (
        "DOGRAH_TRANSFER_DESTINATION",
        "DOGRAH_TRANSFER_ALTERNATE_DESTINATION",
        "QUEUE_HEALTH_URL",
        "QUEUE_HEALTH_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    config = await tcc.find_transfer_call_config(_FakeWorkflow([_FakeNode()]), 1)
    assert config is None
