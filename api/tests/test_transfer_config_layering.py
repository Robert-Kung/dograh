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
    problems = _boot_problems()          # 不得拋 RuntimeError
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
