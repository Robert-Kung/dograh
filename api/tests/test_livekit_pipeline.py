"""Unit tests for the LiveKit pipeline entry (S-L1-PIPELINE).

Covers the audio config LIVEKIT branch and the LiveKit transport builder
signature. Network/SDK connect is not exercised here.
"""

from unittest.mock import AsyncMock, patch

import pytest

from api.enums import WorkflowRunMode
from api.services.pipecat.audio_config import create_audio_config
from api.services.pipecat.transport_setup import create_livekit_transport


def test_audio_config_livekit_uses_16k():
    cfg = create_audio_config(WorkflowRunMode.LIVEKIT.value)
    assert cfg.transport_in_sample_rate == 16000
    assert cfg.transport_out_sample_rate == 16000
    assert cfg.pipeline_sample_rate == 16000


@pytest.mark.asyncio
async def test_create_livekit_transport_builds_with_signature():
    audio_config = create_audio_config(WorkflowRunMode.LIVEKIT.value)

    with (
        patch(
            "api.services.pipecat.transport_setup.build_audio_out_mixer",
            new=AsyncMock(return_value=None),
        ),
        patch("api.services.pipecat.transport_setup.LiveKitTransport") as MockT,
    ):
        await create_livekit_transport(
            "wss://lk.example", "tok", "cs-+886", audio_config, is_realtime=False
        )

    MockT.assert_called_once()
    kwargs = MockT.call_args.kwargs
    assert kwargs["url"] == "wss://lk.example"
    assert kwargs["token"] == "tok"
    assert kwargs["room_name"] == "cs-+886"
    assert kwargs["params"].audio_in_enabled is True
    assert kwargs["params"].audio_out_enabled is True
