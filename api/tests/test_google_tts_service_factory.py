from types import SimpleNamespace
from unittest.mock import patch

from pipecat.services.settings import NOT_GIVEN

from api.services.configuration.registry import ServiceProviders
from api.services.pipecat.service_factory import create_tts_service


def test_create_google_tts_service_uses_credentials_location_and_settings():
    user_config = SimpleNamespace(
        tts=SimpleNamespace(
            provider=ServiceProviders.GOOGLE.value,
            credentials='{"project_id":"demo-project"}',
            api_key=None,
            model="chirp_3_hd",
            voice="en-US-Chirp3-HD-Charon",
            language="en-US",
            speed=1.15,
            location="us-central1",
        )
    )
    audio_config = SimpleNamespace(
        transport_out_sample_rate=24000,
        transport_in_sample_rate=16000,
    )

    with patch("api.services.pipecat.service_factory.GoogleTTSService") as mock_service:
        create_tts_service(user_config, audio_config)

    assert mock_service.call_count == 1
    kwargs = mock_service.call_args.kwargs
    assert kwargs["credentials"] == '{"project_id":"demo-project"}'
    assert kwargs["location"] == "us-central1"
    assert kwargs["settings"].model == "chirp_3_hd"
    assert kwargs["settings"].voice == "en-US-Chirp3-HD-Charon"
    assert kwargs["settings"].language == "en-US"
    assert kwargs["settings"].speaking_rate == 1.15


def test_create_google_tts_service_routes_wavenet_to_http():
    """Non-Chirp3-HD/Journey voices (e.g. the only cmn-TW voices) can't use the
    streaming service — they must route to the HTTP synthesize API."""
    user_config = SimpleNamespace(
        tts=SimpleNamespace(
            provider=ServiceProviders.GOOGLE.value,
            credentials='{"project_id":"demo-project"}',
            api_key=None,
            model="chirp_3_hd",
            voice="cmn-TW-Wavenet-A",
            language="cmn-TW",
            speed=1.15,
            location=None,
        )
    )
    audio_config = SimpleNamespace(
        transport_out_sample_rate=24000,
        transport_in_sample_rate=16000,
    )

    with (
        patch("api.services.pipecat.service_factory.GoogleTTSService") as mock_stream,
        patch("api.services.pipecat.service_factory.GoogleHttpTTSService") as mock_http,
    ):
        create_tts_service(user_config, audio_config)

    assert mock_stream.call_count == 0
    assert mock_http.call_count == 1
    kwargs = mock_http.call_args.kwargs
    assert kwargs["credentials"] == '{"project_id":"demo-project"}'
    assert kwargs["settings"].voice == "cmn-TW-Wavenet-A"
    assert kwargs["settings"].language == "cmn-TW"
    assert kwargs["settings"].speaking_rate == 1.15


def test_create_google_tts_service_journey_voice_streams():
    user_config = SimpleNamespace(
        tts=SimpleNamespace(
            provider=ServiceProviders.GOOGLE.value,
            credentials=None,
            api_key=None,
            model="chirp_3_hd",
            voice="en-US-Journey-D",
            language="en-US",
            speed=1.0,
            location=None,
        )
    )
    audio_config = SimpleNamespace(
        transport_out_sample_rate=24000,
        transport_in_sample_rate=16000,
    )

    with (
        patch("api.services.pipecat.service_factory.GoogleTTSService") as mock_stream,
        patch("api.services.pipecat.service_factory.GoogleHttpTTSService") as mock_http,
    ):
        create_tts_service(user_config, audio_config)

    assert mock_stream.call_count == 1
    assert mock_http.call_count == 0


def test_create_google_tts_service_omits_default_speed():
    user_config = SimpleNamespace(
        tts=SimpleNamespace(
            provider=ServiceProviders.GOOGLE.value,
            credentials=None,
            api_key=None,
            model="chirp_3_hd",
            voice="en-US-Chirp3-HD-Charon",
            language="sw-KE",
            speed=1.0,
            location=None,
        )
    )
    audio_config = SimpleNamespace(
        transport_out_sample_rate=24000,
        transport_in_sample_rate=16000,
    )

    with patch("api.services.pipecat.service_factory.GoogleTTSService") as mock_service:
        create_tts_service(user_config, audio_config)

    assert mock_service.call_count == 1
    kwargs = mock_service.call_args.kwargs
    assert kwargs["location"] is None
    assert kwargs["settings"].model == "chirp_3_hd"
    assert kwargs["settings"].language == "sw-KE"
    assert kwargs["settings"].speaking_rate is NOT_GIVEN
