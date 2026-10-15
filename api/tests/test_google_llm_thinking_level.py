"""Gemini Flash 3.7 起不接受 thinking_level=minimal（pipecat 的低延遲預設）——factory 依型號改送 low。"""

from unittest.mock import patch

import pytest

from api.services.configuration.registry import ServiceProviders
from api.services.pipecat.service_factory import (
    _google_thinking_for_model,
    create_llm_service_from_provider,
)


@pytest.mark.parametrize(
    "model",
    ["gemini-3.7-flash", "gemini-3.8-flash", "gemini-3.10-flash"],
)
def test_flash_models_rejecting_minimal_get_low(model):
    assert _google_thinking_for_model(model).thinking_level == "low"


@pytest.mark.parametrize(
    "model",
    [
        "gemini-2.5-flash",
        "gemini-3.5-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.8-flash-lite",
        "gemini-flash-lite-latest",
        "gemini-flash-latest",
        "gemini-4.0-flash",
        "gemini-3.8-flash-image",
        "gemini-2.5-pro",
        "",
    ],
)
def test_other_models_keep_pipecat_default(model):
    assert _google_thinking_for_model(model) is None


@pytest.mark.parametrize(
    ("provider", "target", "extra"),
    [
        (ServiceProviders.GOOGLE.value, "DograhGoogleLLMService", {"api_key": "k"}),
        (
            ServiceProviders.GOOGLE_VERTEX.value,
            "DograhGoogleVertexLLMService",
            {"api_key": None, "project_id": "p", "credentials": "{}"},
        ),
    ],
)
def test_factory_passes_thinking_to_settings(provider, target, extra):
    with patch(f"api.services.pipecat.service_factory.{target}") as svc:
        create_llm_service_from_provider(
            provider=provider, model="gemini-3.8-flash", **extra
        )
    assert svc.call_args.kwargs["settings"].thinking.thinking_level == "low"

    with patch(f"api.services.pipecat.service_factory.{target}") as svc:
        create_llm_service_from_provider(
            provider=provider, model="gemini-3.6-flash", **extra
        )
    assert svc.call_args.kwargs["settings"].thinking is None
