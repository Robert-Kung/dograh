"""muse-voice-provider 3.1／3.2：Meta Muse STT provider 與 `openai`＋`muse-*` LLM 的建構參數。

鎖住：`muse` 設定類別的金鑰欄位叫 `api_key`（masking 依欄位名遮罩）、設定驗證離線且不連網、
factory 傳給 pipecat 的參數（STT 語言偏好、LLM 的 reasoning_effort 與串流逾時），以及非
`muse-*` 的 openai 模型不受影響。
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from api.schemas.ai_model_configuration import (
    OrganizationAIModelConfigurationV2,
    compile_ai_model_configuration_v2,
)
from api.services.configuration.check_validity import UserConfigurationValidator
from api.services.configuration.masking import SERVICE_SECRET_FIELDS
from api.services.configuration.registry import (
    REGISTRY,
    MuseSTTConfiguration,
    ServiceProviders,
    ServiceType,
)
from api.services.pipecat.service_factory import (
    GEMINI_LLM_FIRST_CHUNK_TIMEOUT_SECS,
    GEMINI_STREAM_IDLE_TIMEOUT_SECS,
    MUSE_FALLBACK_USER_MESSAGE,
    create_llm_service_from_provider,
    create_stt_service,
)

FAKE_KEY = "muse-unit-test-key-not-real-0000"
AUDIO_CONFIG = SimpleNamespace(
    transport_in_sample_rate=16000, transport_out_sample_rate=16000
)


def _muse_stt(**overrides):
    fields = {
        "provider": ServiceProviders.MUSE.value,
        "api_key": FAKE_KEY,
        "model": "muse-voice-transcribe-1.0",
        "language": "Mandarin Chinese,English",
    }
    fields.update(overrides)
    return SimpleNamespace(stt=SimpleNamespace(**fields))


def test_provider_registered_for_stt_only():
    assert ServiceProviders.MUSE.value == "muse"
    assert REGISTRY[ServiceType.STT]["muse"] is MuseSTTConfiguration
    assert "muse" not in REGISTRY[ServiceType.LLM]


def test_credential_field_is_masked_by_name():
    assert "api_key" in MuseSTTConfiguration.model_fields
    assert "api_key" in SERVICE_SECRET_FIELDS
    # No configurable endpoint: the destination is fixed in the service.
    assert "base_url" not in MuseSTTConfiguration.model_fields
    assert "url" not in MuseSTTConfiguration.model_fields


def _pipeline(stt_key=FAKE_KEY):
    return OrganizationAIModelConfigurationV2.model_validate(
        {
            "mode": "byok",
            "byok": {
                "mode": "pipeline",
                "pipeline": {
                    "llm": {
                        "provider": "google",
                        "api_key": "AIza" + "0" * 35,
                        "model": "gemini-3.8-flash",
                    },
                    "stt": {"provider": "muse", "api_key": stt_key},
                    "tts": {"provider": "google_gemini", "api_key": "AIza" + "0" * 35},
                },
            },
        }
    )


@pytest.mark.asyncio
async def test_muse_stt_passes_offline_validation():
    effective = compile_ai_model_configuration_v2(_pipeline())
    assert isinstance(effective.stt, MuseSTTConfiguration)
    assert await UserConfigurationValidator().validate(effective) == {
        "status": [{"model": "all", "message": "ok"}]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "   ", "two words"])
async def test_blank_or_malformed_muse_key_fails_validation(bad):
    effective = compile_ai_model_configuration_v2(_pipeline(stt_key=bad))
    with pytest.raises(ValueError) as exc_info:
        await UserConfigurationValidator().validate(effective)
    assert [status["model"] for status in exc_info.value.args[0]] == ["stt"]


def test_muse_validator_does_not_touch_the_network():
    with patch("socket.socket", side_effect=AssertionError("network used")):
        assert UserConfigurationValidator()._check_muse_api_key("m", FAKE_KEY) is True


def test_muse_stt_factory_passes_key_model_and_language_bias():
    with patch("api.services.pipecat.service_factory.MuseSTTService") as mock_muse:
        create_stt_service(_muse_stt(), AUDIO_CONFIG)
    kwargs = mock_muse.call_args.kwargs
    assert kwargs["api_key"] == FAKE_KEY
    assert kwargs["sample_rate"] == 16000
    assert kwargs["settings"].model == "muse-voice-transcribe-1.0"
    assert kwargs["settings"].language_bias == ["Mandarin Chinese", "English"]


@pytest.mark.parametrize("blank", ["", "  ", None])
def test_muse_stt_factory_blank_language_keeps_service_default(blank):
    with patch("api.services.pipecat.service_factory.MuseSTTService") as mock_muse:
        create_stt_service(_muse_stt(language=blank), AUDIO_CONFIG)
    from pipecat.services.settings import is_given

    assert not is_given(mock_muse.call_args.kwargs["settings"].language_bias)


def test_muse_llm_gets_minimal_reasoning_and_stream_timeouts():
    with patch("api.services.pipecat.service_factory.OpenAILLMService") as mock_llm:
        create_llm_service_from_provider(
            ServiceProviders.OPENAI.value,
            "muse-ember-1.0",
            FAKE_KEY,
            base_url="https://api.meta.ai/v1",
        )
    kwargs = mock_llm.call_args.kwargs
    assert kwargs["base_url"] == "https://api.meta.ai/v1"
    assert kwargs["settings"].extra == {"reasoning_effort": "minimal"}
    assert "verbosity" not in kwargs["settings"].extra
    assert kwargs["first_chunk_timeout_secs"] == GEMINI_LLM_FIRST_CHUNK_TIMEOUT_SECS
    assert kwargs["stream_idle_timeout_secs"] == GEMINI_STREAM_IDLE_TIMEOUT_SECS
    # Muse 拒收沒有 user／tool 的請求（開場只有 system）：factory 交一句固定的觸發語給 pipecat 補上
    assert kwargs["fallback_user_message"] == MUSE_FALLBACK_USER_MESSAGE


def test_other_openai_models_are_unchanged():
    with patch("api.services.pipecat.service_factory.OpenAILLMService") as mock_llm:
        create_llm_service_from_provider(
            ServiceProviders.OPENAI.value, "gpt-4.1", FAKE_KEY
        )
    kwargs = mock_llm.call_args.kwargs
    assert "first_chunk_timeout_secs" not in kwargs
    assert "fallback_user_message" not in kwargs
    assert (
        kwargs["settings"].extra in ({}, None)
        or "reasoning_effort" not in kwargs["settings"].extra
    )


def test_real_muse_stt_service_uses_only_the_given_key(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "env-key-must-not-be-used")
    monkeypatch.setenv("META_API_KEY", "env-key-must-not-be-used")
    service = create_stt_service(_muse_stt(), AUDIO_CONFIG)
    assert service._api_key == FAKE_KEY
    with pytest.raises(ValueError):
        create_stt_service(_muse_stt(api_key="  "), AUDIO_CONFIG)


def test_every_key_of_a_multi_key_stt_config_is_registered_for_redaction():
    # 多金鑰設定讀 api_key 每次都隨機抽一把；登記遮罩與建服務各抽一次會對不上（review S1）
    from api.utils.secret_redaction import redact_secrets

    keys = ["muse-multi-key-one-not-real-0001", "muse-multi-key-two-not-real-0002"]
    stt = MuseSTTConfiguration(
        provider="muse", api_key=keys, model="muse-voice-transcribe-1.0"
    )
    with patch("api.services.pipecat.service_factory.MuseSTTService"):
        create_stt_service(SimpleNamespace(stt=stt), AUDIO_CONFIG)
    assert redact_secrets(f"{keys[0]} {keys[1]}") == "[REDACTED] [REDACTED]"


@pytest.mark.parametrize(
    "base_url",
    [
        None,  # SDK 預設＝api.openai.com
        "https://api.openai.com/v1",
        "https://relay/api.meta.ai/v1",
        "https://api.meta.ai.example.com/v1",
    ],
)
def test_muse_llm_refuses_any_endpoint_but_api_meta_ai(base_url):
    # muse-* 帶的是 Muse 金鑰與 Muse 專用參數，只能送 api.meta.ai（review S4）
    with patch("api.services.pipecat.service_factory.OpenAILLMService") as mock_llm:
        with pytest.raises(ValueError, match="api.meta.ai"):
            create_llm_service_from_provider(
                ServiceProviders.OPENAI.value,
                "muse-ember-1.0",
                FAKE_KEY,
                base_url=base_url,
            )
    mock_llm.assert_not_called()
