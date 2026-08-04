from types import SimpleNamespace
from unittest.mock import patch

from api.services.configuration.registry import (
    DeepgramSTTConfiguration,
    ServiceProviders,
)
from api.services.pipecat.service_factory import create_stt_service


def _user_config(**stt_overrides):
    stt = SimpleNamespace(
        provider=ServiceProviders.DEEPGRAM.value,
        api_key="dg-test-key",
        model="nova-3-general",
        language="multi",
        **stt_overrides,
    )
    return SimpleNamespace(stt=stt)


def test_deepgram_stt_configuration_persists_base_url():
    config = DeepgramSTTConfiguration(
        api_key="dg-test-key",
        base_url="ws://egress-proxy:8443",
    )

    assert config.model_dump()["base_url"] == "ws://egress-proxy:8443"


def test_create_deepgram_stt_service_passes_base_url():
    audio_config = SimpleNamespace(transport_in_sample_rate=16000)

    with patch(
        "api.services.pipecat.service_factory.DeepgramSTTService"
    ) as mock_service:
        create_stt_service(
            _user_config(base_url="ws://egress-proxy:8443"), audio_config
        )

    assert mock_service.call_count == 1
    kwargs = mock_service.call_args.kwargs
    assert kwargs["base_url"] == "ws://egress-proxy:8443"
    assert kwargs["api_key"] == "dg-test-key"


def test_create_deepgram_stt_service_omits_blank_base_url():
    audio_config = SimpleNamespace(transport_in_sample_rate=16000)

    with patch(
        "api.services.pipecat.service_factory.DeepgramSTTService"
    ) as mock_service:
        create_stt_service(_user_config(base_url=""), audio_config)

    assert mock_service.call_count == 1
    assert "base_url" not in mock_service.call_args.kwargs
