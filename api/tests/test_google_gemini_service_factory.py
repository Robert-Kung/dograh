"""gemini-key-voice 1.2：`google_gemini` provider（Gemini API 金鑰的 STT／TTS）與 `google` LLM 的單一金鑰路徑。

CEO 在介面上把 STT、TTS 選 `google_gemini`、LLM 選 `google`＋`gemini-3.8-flash`，三者填同一把
Gemini API 金鑰。本檔鎖住：設定類別只有 `api_key` 憑證欄位、選項用 Gemini API 名稱、factory
傳給 pipecat 的建構參數（TTS 必帶 `use_genai=True`）、空金鑰直接報錯不落到環境變數／ADC
（SEC-8），以及既有 `google`（Cloud）STT／TTS 不受影響。
"""

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import TypeAdapter, ValidationError

from api.schemas.ai_model_configuration import (
    OrganizationAIModelConfigurationV2,
    compile_ai_model_configuration_v2,
)
from api.services.configuration.check_validity import UserConfigurationValidator
from api.services.configuration.options import GOOGLE_MODELS
from api.services.configuration.options.google import (
    GOOGLE_GEMINI_STT_LANGUAGES,
    GOOGLE_GEMINI_STT_MODELS,
    GOOGLE_GEMINI_TTS_LANGUAGES,
    GOOGLE_GEMINI_TTS_MODELS,
    GOOGLE_GEMINI_TTS_VOICES,
)
from api.services.configuration.registry import (
    REGISTRY,
    GoogleGeminiSTTConfiguration,
    GoogleGeminiTTSConfiguration,
    GoogleLLMService,
    ServiceProviders,
    ServiceType,
    STTConfig,
    TTSConfig,
)
from api.services.pipecat.service_factory import (
    create_llm_service_from_provider,
    create_stt_service,
    create_tts_service,
)

# 契約檔的 1.1／1.8 單元測試假值（design D8 掃描規則用），不是真金鑰。
FAKE_KEY = "AIza" + "0" * 35

# 空金鑰時 google-genai 會去讀的環境變數；測試期間全部設成「有值」，確認不會被拿來用。
HOSTILE_ENV = {
    "GOOGLE_API_KEY": "env-google-key",
    "GEMINI_API_KEY": "env-gemini-key",
    "GOOGLE_GENAI_USE_VERTEXAI": "true",
    "GOOGLE_CLOUD_PROJECT": "env-project",
}

AUDIO_CONFIG = SimpleNamespace(
    transport_in_sample_rate=16000, transport_out_sample_rate=16000
)


def _gemini_tts(**overrides):
    fields = {
        "provider": ServiceProviders.GOOGLE_GEMINI.value,
        "api_key": FAKE_KEY,
        "model": "gemini-3.8-flash-lite-tts",
        "voice": "Kore",
        "language": "cmn-TW",
    }
    fields.update(overrides)
    return SimpleNamespace(tts=SimpleNamespace(**fields))


def _gemini_stt(**overrides):
    fields = {
        "provider": ServiceProviders.GOOGLE_GEMINI.value,
        "api_key": FAKE_KEY,
        "model": "gemini-3.5-transcribe-live",
        "language": "zh-TW",
    }
    fields.update(overrides)
    return SimpleNamespace(stt=SimpleNamespace(**fields))


# --- 設定類別與選項 ------------------------------------------------------------


def test_provider_value_and_registration():
    assert ServiceProviders.GOOGLE_GEMINI.value == "google_gemini"
    assert REGISTRY[ServiceType.TTS]["google_gemini"] is GoogleGeminiTTSConfiguration
    assert REGISTRY[ServiceType.STT]["google_gemini"] is GoogleGeminiSTTConfiguration
    # 只有 STT／TTS；LLM 沿用既有 `google`。
    assert "google_gemini" not in REGISTRY[ServiceType.LLM]


@pytest.mark.parametrize(
    "config_cls", [GoogleGeminiTTSConfiguration, GoogleGeminiSTTConfiguration]
)
def test_gemini_config_has_only_api_key_credential(config_cls):
    fields = set(config_cls.model_fields)
    assert "api_key" in fields
    assert "credentials" not in fields
    assert "location" not in fields
    assert config_cls.model_fields["api_key"].is_required()
    with pytest.raises(ValidationError):
        config_cls()


def test_gemini_tts_defaults_use_gemini_api_names():
    config = GoogleGeminiTTSConfiguration(api_key=FAKE_KEY)
    assert config.provider == ServiceProviders.GOOGLE_GEMINI
    # Cloud TTS 的名稱（fork 的預設）不能拿來打 Gemini API。
    assert config.model != "gemini-2.5-flash-tts"
    assert config.model in GOOGLE_GEMINI_TTS_MODELS
    assert "gemini-2.5-flash-tts" not in GOOGLE_GEMINI_TTS_MODELS
    assert config.voice in GOOGLE_GEMINI_TTS_VOICES
    assert config.language == "cmn-TW"
    assert config.language in GOOGLE_GEMINI_TTS_LANGUAGES
    # Cloud TTS 的 voice 名稱與 Gemini 不通用（design D2）。
    assert not any("-" in voice for voice in GOOGLE_GEMINI_TTS_VOICES)


def test_gemini_stt_defaults():
    config = GoogleGeminiSTTConfiguration(api_key=FAKE_KEY)
    assert config.provider == ServiceProviders.GOOGLE_GEMINI
    assert config.model in GOOGLE_GEMINI_STT_MODELS
    assert config.language == "zh-TW"
    assert config.language in GOOGLE_GEMINI_STT_LANGUAGES


def test_gemini_schema_offers_options_to_the_ui():
    tts_props = GoogleGeminiTTSConfiguration.model_json_schema()["properties"]
    assert tuple(tts_props["model"]["examples"]) == GOOGLE_GEMINI_TTS_MODELS
    assert tuple(tts_props["voice"]["examples"]) == GOOGLE_GEMINI_TTS_VOICES
    assert tuple(tts_props["language"]["examples"]) == GOOGLE_GEMINI_TTS_LANGUAGES
    stt_props = GoogleGeminiSTTConfiguration.model_json_schema()["properties"]
    assert tuple(stt_props["model"]["examples"]) == GOOGLE_GEMINI_STT_MODELS
    assert tuple(stt_props["language"]["examples"]) == GOOGLE_GEMINI_STT_LANGUAGES


def test_discriminated_unions_resolve_google_gemini():
    tts = TypeAdapter(TTSConfig).validate_python(
        {"provider": "google_gemini", "api_key": FAKE_KEY}
    )
    stt = TypeAdapter(STTConfig).validate_python(
        {"provider": "google_gemini", "api_key": FAKE_KEY}
    )
    assert isinstance(tts, GoogleGeminiTTSConfiguration)
    assert isinstance(stt, GoogleGeminiSTTConfiguration)


def test_google_llm_offers_gemini_3_8_flash():
    assert "gemini-3.8-flash" in GOOGLE_MODELS
    examples = GoogleLLMService.model_json_schema()["properties"]["model"]["examples"]
    assert "gemini-3.8-flash" in examples


# --- 存檔驗證（UI 存設定時跑） --------------------------------------------------


def _byok_pipeline(stt_key=FAKE_KEY, tts_key=FAKE_KEY):
    return OrganizationAIModelConfigurationV2.model_validate(
        {
            "mode": "byok",
            "byok": {
                "mode": "pipeline",
                "pipeline": {
                    "llm": {
                        "provider": "google",
                        "api_key": FAKE_KEY,
                        "model": "gemini-3.8-flash",
                    },
                    "stt": {"provider": "google_gemini", "api_key": stt_key},
                    "tts": {"provider": "google_gemini", "api_key": tts_key},
                },
            },
        }
    )


@pytest.mark.asyncio
async def test_single_key_pipeline_passes_validation():
    effective = compile_ai_model_configuration_v2(_byok_pipeline())
    assert isinstance(effective.stt, GoogleGeminiSTTConfiguration)
    assert isinstance(effective.tts, GoogleGeminiTTSConfiguration)
    assert await UserConfigurationValidator().validate(effective) == {
        "status": [{"model": "all", "message": "ok"}]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", ["", "   "])
async def test_blank_gemini_key_fails_validation(blank):
    effective = compile_ai_model_configuration_v2(
        _byok_pipeline(stt_key=blank, tts_key=blank)
    )
    with pytest.raises(ValueError) as exc_info:
        await UserConfigurationValidator().validate(effective)
    assert [status["model"] for status in exc_info.value.args[0]] == ["stt", "tts"]


# --- service_factory：建構參數（pipecat 類別以 mock 取代） ---------------------


def test_gemini_tts_factory_passes_key_use_genai_and_settings():
    with (
        patch("api.services.pipecat.service_factory.GeminiTTSService") as mock_gemini,
        patch("api.services.pipecat.service_factory.GoogleTTSService") as mock_stream,
        patch("api.services.pipecat.service_factory.GoogleHttpTTSService") as mock_http,
    ):
        create_tts_service(_gemini_tts(), AUDIO_CONFIG)

    assert mock_stream.call_count == 0
    assert mock_http.call_count == 0
    assert mock_gemini.call_count == 1
    kwargs = mock_gemini.call_args.kwargs
    assert kwargs["api_key"] == FAKE_KEY
    assert kwargs["use_genai"] is True
    assert "credentials" not in kwargs
    # Gemini API 固定回 24 kHz PCM；標成 24 kHz 由輸出 transport 重採樣。
    assert kwargs["sample_rate"] == 24000
    assert kwargs["settings"].model == "gemini-3.8-flash-lite-tts"
    assert kwargs["settings"].voice == "Kore"
    assert kwargs["settings"].language == "cmn-TW"


@pytest.mark.parametrize("blank", ["", "  ", None])
def test_gemini_tts_factory_blank_language_lets_model_detect(blank):
    with patch("api.services.pipecat.service_factory.GeminiTTSService") as mock_gemini:
        create_tts_service(_gemini_tts(language=blank), AUDIO_CONFIG)
    assert mock_gemini.call_args.kwargs["settings"].language is None


def test_gemini_stt_factory_passes_key_model_and_language_hint():
    with (
        patch("api.services.pipecat.service_factory.GeminiSTTService") as mock_gemini,
        patch("api.services.pipecat.service_factory.GoogleSTTService") as mock_cloud,
    ):
        create_stt_service(_gemini_stt(), AUDIO_CONFIG)

    assert mock_cloud.call_count == 0
    assert mock_gemini.call_count == 1
    kwargs = mock_gemini.call_args.kwargs
    assert kwargs["api_key"] == FAKE_KEY
    assert "credentials" not in kwargs
    assert kwargs["sample_rate"] == 16000
    assert kwargs["settings"].model == "gemini-3.5-transcribe-live"
    # 原樣送出介面上填的語言碼（不經 pipecat 的 Cloud STT 語言對照）。
    assert kwargs["settings"].languages == ["zh-TW"]


@pytest.mark.parametrize("blank", ["", "  ", None])
def test_gemini_stt_factory_blank_language_lets_model_detect(blank):
    with patch("api.services.pipecat.service_factory.GeminiSTTService") as mock_gemini:
        create_stt_service(_gemini_stt(language=blank), AUDIO_CONFIG)
    assert mock_gemini.call_args.kwargs["settings"].languages == []


def test_google_cloud_tts_and_stt_do_not_route_to_gemini():
    cloud_tts = SimpleNamespace(
        tts=SimpleNamespace(
            provider=ServiceProviders.GOOGLE.value,
            credentials='{"project_id":"demo-project"}',
            api_key=None,
            model="chirp_3_hd",
            voice="cmn-TW-Wavenet-A",
            language="cmn-TW",
            speed=1.0,
            location=None,
        )
    )
    cloud_stt = SimpleNamespace(
        stt=SimpleNamespace(
            provider=ServiceProviders.GOOGLE.value,
            credentials='{"project_id":"demo-project"}',
            api_key=None,
            model="chirp_3",
            language="cmn-Hant-TW",
            location="global",
        )
    )
    with (
        patch(
            "api.services.pipecat.service_factory.GeminiTTSService"
        ) as mock_gemini_tts,
        patch(
            "api.services.pipecat.service_factory.GeminiSTTService"
        ) as mock_gemini_stt,
        patch("api.services.pipecat.service_factory.GoogleHttpTTSService") as mock_http,
        patch(
            "api.services.pipecat.service_factory.GoogleSTTService"
        ) as mock_cloud_stt,
    ):
        create_tts_service(cloud_tts, AUDIO_CONFIG)
        create_stt_service(cloud_stt, AUDIO_CONFIG)

    assert mock_gemini_tts.call_count == 0
    assert mock_gemini_stt.call_count == 0
    assert mock_http.call_args.kwargs["credentials"] == '{"project_id":"demo-project"}'
    assert (
        mock_cloud_stt.call_args.kwargs["credentials"]
        == '{"project_id":"demo-project"}'
    )


# --- 真的 pipecat 類別：空金鑰報錯、不讀環境變數／不走 Vertex（SEC-8） ----------


@pytest.mark.parametrize("blank", ["", "   "])
def test_gemini_tts_blank_key_raises(blank):
    with (
        patch.dict(os.environ, HOSTILE_ENV),
        pytest.raises(ValueError, match="non-empty api_key"),
    ):
        create_tts_service(_gemini_tts(api_key=blank), AUDIO_CONFIG)


@pytest.mark.parametrize("blank", ["", "   "])
def test_gemini_stt_blank_key_raises(blank):
    with (
        patch.dict(os.environ, HOSTILE_ENV),
        pytest.raises(ValueError, match="non-empty api_key"),
    ):
        create_stt_service(_gemini_stt(api_key=blank), AUDIO_CONFIG)


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_google_llm_blank_key_raises(blank):
    with (
        patch.dict(os.environ, HOSTILE_ENV),
        pytest.raises(ValueError, match="non-empty api_key"),
    ):
        create_llm_service_from_provider(
            provider=ServiceProviders.GOOGLE.value,
            model="gemini-3.8-flash",
            api_key=blank,
        )


def test_gemini_tts_real_service_uses_only_the_given_key():
    with patch.dict(os.environ, HOSTILE_ENV):
        tts = create_tts_service(_gemini_tts(), AUDIO_CONFIG)
    assert tts._use_genai is True
    assert tts._api_key == FAKE_KEY
    assert tts._client._api_client.vertexai is False
    assert tts._client._api_client.api_key == FAKE_KEY
    assert tts._settings.model == "gemini-3.8-flash-lite-tts"
    assert tts._settings.voice == "Kore"
    assert tts._settings.language == "cmn-TW"


def test_gemini_tts_real_service_blank_language_sends_none():
    with patch.dict(os.environ, HOSTILE_ENV):
        tts = create_tts_service(_gemini_tts(language=""), AUDIO_CONFIG)
    assert tts._settings.language is None


def test_gemini_stt_real_service_uses_only_the_given_key():
    with patch.dict(os.environ, HOSTILE_ENV):
        stt = create_stt_service(_gemini_stt(), AUDIO_CONFIG)
    assert stt._client._api_client.vertexai is False
    assert stt._client._api_client.api_key == FAKE_KEY
    assert stt._settings.model == "gemini-3.5-transcribe-live"
    assert stt._get_language_codes() == ["zh-TW"]


def test_google_llm_real_service_uses_only_the_given_key():
    with patch.dict(os.environ, HOSTILE_ENV):
        llm = create_llm_service_from_provider(
            provider=ServiceProviders.GOOGLE.value,
            model="gemini-3.8-flash",
            api_key=FAKE_KEY,
        )
    assert llm._client._api_client.vertexai is False
    assert llm._client._api_client.api_key == FAKE_KEY
    # thinking_level 沿用 Flash 3.7+ 規則（test_google_llm_thinking_level.py）。
    assert llm._settings.thinking.thinking_level == "low"
