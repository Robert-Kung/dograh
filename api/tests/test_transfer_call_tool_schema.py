"""Transfer-call tool schema carries the transfer-gate keys (fork PR #11).

The in-call gate (press0_gate / execute_cold_transfer / queue_is_healthy) reads
schedule- and queue-health keys from the persisted tool config. Persistence goes
through ``request.definition.model_dump()`` (tool_management.create_tool_for_user),
so any key missing from ``TransferCallConfig`` is silently dropped on write while
the API returns 200 — the gate then runs unconfigured. These tests pin the
round-trip for every key the gate reads.
"""

import pytest
from pydantic import ValidationError

from api.schemas.tool import CreateToolRequest, TransferCallConfig
from api.tests.support.platform_artifacts import requires_sip_uri

GATE_CONFIG = {
    # W2a D-A6: the ARI dialect ("sip/queue@…", "PJSIP/1234", bare E.164)
    # no longer validates. It never dialled on LiveKit; accepting it only
    # produced destinations that failed at REFER time.
    "destination": "sip:queue@example.internal",
    "schedule": {
        "timezone": "Asia/Taipei",
        "weekly": {"mon": [["09:00", "18:00"]]},
    },
    "afterHoursAction": "back_to_ai",
    "afterHoursMessage": "目前非營業時間",
    "alternateDestination": "tel:+886223456789",
    "transferFailedMessage": "轉接失敗",
    "transferUnavailableMessage": "目前無法轉接",
    "unavailableAnnounceLimit": 2,
    "queueHealthUrl": "http://queue:8080/health",
    "queueHealthToken": "token-value",
    "queueHealthTimeoutSeconds": 2.5,
    "queueHealthCacheTtlSeconds": 5.0,
}


def _create_request(config: dict) -> CreateToolRequest:
    return CreateToolRequest.model_validate(
        {
            "name": "transfer to human queue",
            "definition": {
                "schema_version": 1,
                "type": "transfer_call",
                "config": config,
            },
        }
    )


@requires_sip_uri
def test_gate_keys_survive_the_persistence_dump():
    """The exact failure shape this PR fixes: write-path silently dropping keys."""
    request = _create_request(GATE_CONFIG)
    dumped = request.definition.model_dump()["config"]
    for key, want in GATE_CONFIG.items():
        assert dumped[key] == want, f"gate key dropped on model_dump: {key}"


@requires_sip_uri
def test_config_without_gate_keys_still_validates():
    """Pre-existing tools (destination-only) keep working unchanged."""
    request = _create_request({"destination": "tel:+886223456789"})
    dumped = request.definition.model_dump()["config"]
    assert dumped["destination"] == "tel:+886223456789"
    # unset gate keys dump as None, which every reader treats as unconfigured
    assert dumped["queueHealthUrl"] is None
    assert dumped["schedule"] is None


@requires_sip_uri
def test_alternate_destination_validated_like_destination():
    bad = dict(GATE_CONFIG, alternateDestination="not-a-destination")
    # ``match`` pins the *reason* (review M-7). Without the shared parser the
    # write path refuses every destination, so a bare ``raises`` would pass
    # while proving nothing about ``alternateDestination`` at all.
    with pytest.raises(ValidationError, match="Invalid transfer destination"):
        _create_request(bad)


@requires_sip_uri
def test_alternate_destination_accepts_sip_and_none():
    assert (
        TransferCallConfig.model_validate(
            {
                "destination": "tel:+886223456789",
                "alternateDestination": "sip:human@pbx.example",
            }
        ).alternateDestination
        == "sip:human@pbx.example"
    )
    assert (
        TransferCallConfig.model_validate(
            {"destination": "tel:+886223456789"}
        ).alternateDestination
        is None
    )


@requires_sip_uri
def test_unavailable_announce_limit_must_be_positive():
    # Same reason-pinning as above: ``GATE_CONFIG`` carries a destination, so
    # an unmounted parser fails this request before the limit is ever looked at.
    with pytest.raises(ValidationError, match="unavailableAnnounceLimit"):
        _create_request(dict(GATE_CONFIG, unavailableAnnounceLimit=0))


# ── W3a §1.3：``destination`` 由必填改選填 ──────────────────────────────


@requires_sip_uri
def test_speech_layer_only_config_validates():
    """本 change 的整條遷移鏈掛在這一條上。

    版控範本移除部署層六欄之後，``ensure_tools`` 送出的 PUT 就長這個樣子。
    ``destination`` 若仍是 ``str = Field(...)``（無 default ＝ 必填），這個 body
    會被上游 pydantic 以 **422** 擋下——bootstrap 自我阻擋，整套部署卡在第 2 步，
    而症狀（「部署跑不起來」）與病因（一個 schema 註記）隔了三個檔案。
    """
    speech_only = {
        "messageType": "custom",
        "customMessage": "正在為您轉接真人客服，請稍候。",
        "timeout": 30,
        "afterHoursAction": "back_to_ai",
        "afterHoursMessage": "目前是非營業時間。",
        "transferFailedMessage": "轉接暫時沒有成功。",
        "transferUnavailableMessage": "真人客服目前無法接聽。",
        "unavailableAnnounceLimit": 2,
    }
    dumped = _create_request(speech_only).definition.model_dump()["config"]
    for key, want in speech_only.items():
        assert dumped[key] == want
    # 六欄全部以 None 落庫（``model_dump`` 預設 ``exclude_none=False``）——
    # 這正是 §3 要清的殘留形狀，這裡先把它釘住。
    for key in (
        "destination",
        "alternateDestination",
        "queueHealthUrl",
        "queueHealthToken",
        "queueHealthTimeoutSeconds",
        "queueHealthCacheTtlSeconds",
    ):
        assert dumped[key] is None


@requires_sip_uri
def test_blank_destination_is_still_refused():
    """選填 ≠ 可以是空字串。

    缺席會退到部署層；**空字串會裝上一個撥不出去的轉接**（W0 的失效形狀），
    而它形狀上是「有值」，所以不會落進任何「未設定」分支。兩者不是同一件事，
    只有後者是這個模型該拒的寫入。
    """
    with pytest.raises(ValidationError, match="must not be blank"):
        _create_request(dict(GATE_CONFIG, destination="   "))


@requires_sip_uri
def test_absent_destination_is_not_confused_with_blank():
    """對照組：上一條的訊息 MUST NOT 在「缺席」時也出現。

    沒有這條對照，上一條在「兩種都拒」的實作下**照樣綠**，而那個實作就是
    422 自我阻擋本身。
    """
    request = _create_request({"messageType": "none"})
    assert request.definition.config.destination is None
