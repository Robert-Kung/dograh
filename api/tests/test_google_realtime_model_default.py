"""gemini-key-voice 2.1: realtime defaults to Gemini 3.8 Live (design D4)."""

from api.services.configuration.options.google import GOOGLE_REALTIME_MODELS
from api.services.configuration.registry import GoogleRealtimeLLMConfiguration


def test_default_realtime_model_is_gemini_3_8_live():
    assert GoogleRealtimeLLMConfiguration(api_key="test-key").model == "gemini-3.8-live"


def test_previous_live_model_stays_selectable():
    # Saved configurations that pinned the preview model must still validate.
    assert GOOGLE_REALTIME_MODELS[0] == "gemini-3.8-live"
    assert "gemini-3.1-flash-live-preview" in GOOGLE_REALTIME_MODELS
    cfg = GoogleRealtimeLLMConfiguration(
        api_key="test-key", model="gemini-3.1-flash-live-preview"
    )
    assert cfg.model == "gemini-3.1-flash-live-preview"
