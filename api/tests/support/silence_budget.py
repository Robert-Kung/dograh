"""The design D9 silence budget of the LIVEKIT idle path, from the code's constants.

On the idle path the caller says nothing: the bot's last audio ends, pipecat's
output transport waits ``BOT_VAD_STOP_SECS`` before ``BotStoppedSpeakingFrame``
starts the idle clock, and after the LIVEKIT idle timeout the idle prompt asks
the LLM for a reply (which also arms bot_silence, 5.1 fix review R2). What
follows depends on what fails; each budget test adds the terms of its branch
to :func:`idle_path_silence` and asserts the total is within
:data:`SILENCE_BUDGET`.

Every term is read from the code, so changing a timeout re-checks the sums. A
workflow's ``run_configs`` and the ``SAFETYNET_MAX_SILENCE_SECONDS`` env can
still lengthen the path at run time; the sums use the defaults.
"""

import inspect

from pipecat.transports.base_output import BOT_VAD_STOP_SECS

from api.services.pipecat import livekit_safetynet as sn
from api.services.pipecat.run_pipeline import LIVEKIT_MAX_USER_IDLE_TIMEOUT_SECS

# design D9: TTS-unavailable continuous silence, provisional cap (1.4).
SILENCE_BUDGET = 20.0


def watchdog_poll_seconds() -> float:
    """How late bot_silence can fire after its deadline: the monitor's poll."""
    return (
        inspect.signature(sn.SafetynetWatchdog.__init__)
        .parameters["poll_seconds"]
        .default
    )


def idle_path_silence(*after_idle_prompt: float) -> float:
    """Silence from the bot's last audio to the exit, on the LIVEKIT idle turn.

    Args:
        *after_idle_prompt: The branch's terms after the idle prompt goes out
            (detection, poll delay, playback timeout).
    """
    return (
        LIVEKIT_MAX_USER_IDLE_TIMEOUT_SECS + BOT_VAD_STOP_SECS + sum(after_idle_prompt)
    )
