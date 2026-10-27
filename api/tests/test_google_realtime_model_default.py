"""gemini-key-voice 2.1: realtime defaults to Gemini 3.8 Live (design D4)."""

from api.services.configuration.options.google import GOOGLE_REALTIME_MODELS
from api.services.configuration.registry import GoogleRealtimeLLMConfiguration


def test_default_realtime_model_is_gemini_3_8_live():
    assert GoogleRealtimeLLMConfiguration(api_key="test-key").model == "gemini-3.8-live"


def test_both_live_models_are_offered():
    # The editor lists these; saved configurations pinned to the preview model
    # keep showing it as a known choice.
    assert {"gemini-3.8-live", "gemini-3.1-flash-live-preview"} <= set(
        GOOGLE_REALTIME_MODELS
    )
