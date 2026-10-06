"""Recording consent gate (S-L8-RECORD, PDPA).

dograh already records LIVEKIT calls (audio buffers → storage); this module
adds the compliance layer in front: play a recording notice before any
conversation, keep a consent record, and — fail-safe — produce **no recording
at all** when the notice was not configured or could not be played. The
notice audio itself lands at the head of the recording, self-evidencing.
Transcripts are unaffected (necessary service processing, not enhanced
collection); their retention is set separately and explicitly (ccp W4c).

Consent model is notice-based: the caller continuing after the notice is
consent. The consent record (``consent_notice`` in workflow_run annotations)
is compliance evidence and is never deleted with the recording.
"""

import os
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

_DEFAULT_SCRIPT_VERSION = "draft-0"
_DEFAULT_AUDIO_RETENTION_DAYS = 180
NEVER = "never"


def consent_notice_text() -> Optional[str]:
    value = (os.environ.get("RECORD_CONSENT_NOTICE_TEXT") or "").strip()
    return value or None


def consent_script_version() -> str:
    return os.environ.get("RECORD_CONSENT_SCRIPT_VERSION", _DEFAULT_SCRIPT_VERSION)


def audio_retention_days() -> int:
    return int(
        os.environ.get("RECORD_AUDIO_RETENTION_DAYS", _DEFAULT_AUDIO_RETENTION_DAYS)
    )


def transcript_retention() -> int | str | None:
    """Days, ``"never"``, or None when unset or malformed.

    Required at deploy time (preflight refuses a missing value); at runtime a
    missing value only skips the transcript sweep — dograh still starts (C4).
    """
    value = (os.environ.get("RECORD_TRANSCRIPT_RETENTION_DAYS") or "").strip()
    if value == NEVER:
        return NEVER
    if not value.isascii() or not value.isdigit():
        return None
    days = int(value)
    return days if days > 0 else None


def validate_recording_config() -> None:
    """Fail loudly at startup on malformed recording config."""
    try:
        days = audio_retention_days()
    except ValueError as e:
        raise RuntimeError(f"RECORD_AUDIO_RETENTION_DAYS is not a number: {e}") from e
    if days <= 0:
        raise RuntimeError(f"RECORD_AUDIO_RETENTION_DAYS must be > 0, got {days}")


def log_consent_event(
    event: str,
    *,
    room_name: str,
    reason: str,
    workflow_run_id: Optional[int] = None,
) -> None:
    """Structured ``consent.*`` events via the unified call-event path."""
    from api.services.observability.call_events import emit

    emit(event, room_name=room_name, reason=reason, workflow_run_id=workflow_run_id)


NOTICE_FAILURE_REASONS = (
    "realtime_no_tts_path",
    "audio_greeting_ordering_unsupported",
    "playback_error",
)


class RecordingConsentGate:
    """Plays the recording notice and gates recording on its outcome (C4).

    ``should_record`` stays False until the notice was successfully queued;
    a playback failure logs ``consent.notice_failed`` and the call proceeds
    unrecorded — never interrupted.
    """

    def __init__(
        self, engine, *, room_name: str, workflow_run_id: int, is_realtime: bool = False
    ):
        self._engine = engine
        self._room_name = room_name
        self._workflow_run_id = workflow_run_id
        self._is_realtime = is_realtime
        self._notice_played = False

    @property
    def room_name(self) -> str:
        return self._room_name

    @property
    def should_record(self) -> bool:
        return self._notice_played

    def _start_greeting_is_audio(self) -> bool:
        try:
            workflow = getattr(self._engine, "workflow", None)
            if workflow is None:
                return False
            info = self._engine.get_node_greeting(workflow.start_node_id)
            return bool(info) and info[0] == "audio"
        except Exception:
            return False

    async def _persist(self, record: dict) -> None:
        """Write the consent record on the run; a failure never reaches the call."""
        try:
            from api.db import db_client

            await db_client.update_workflow_run(
                self._workflow_run_id, annotations={"consent_notice": record}
            )
        except Exception as e:
            logger.error(f"failed to persist consent record: {type(e).__name__}")

    async def _failed(self, reason: str) -> None:
        # reason is one of NOTICE_FAILURE_REASONS — never an exception message
        log_consent_event(
            "consent.notice_failed",
            room_name=self._room_name,
            reason=reason,
            workflow_run_id=self._workflow_run_id,
        )
        await self._persist(
            {"failed_at": datetime.now(timezone.utc).isoformat(), "failed_reason": reason}
        )

    async def play_notice(self) -> None:
        text = consent_notice_text()
        if text is None:
            logger.info(
                f"RECORD_CONSENT_NOTICE_TEXT not set; call {self._workflow_run_id} "
                "proceeds without notice and without recording (fail-safe)"
            )
            # ccp W4c: "recording was off" is read from this marker, never
            # inferred from a missing consent record.
            await self._persist({"disabled": True})
            return
        if self._is_realtime:
            # Speech-to-speech pipelines have no TTS service — a queued
            # TTSSpeakFrame would be silently dropped while we wrongly record
            # consent. Fail-safe: no notice, no recording.
            await self._failed("realtime_no_tts_path")
            return
        if self._start_greeting_is_audio():
            # Audio greetings inject via the transport output queue, bypassing
            # the pipeline FIFO the notice rides on — notice-before-greeting
            # can't be guaranteed. Fail-safe: no notice, no recording.
            # (MVP inbound workflows must use text greetings; S-L2-AGENT.)
            await self._failed("audio_greeting_ordering_unsupported")
            return
        try:
            from pipecat.frames.frames import TTSSpeakFrame

            await self._engine.task.queue_frame(
                TTSSpeakFrame(text, persist_to_logs=True)
            )
        except Exception as e:
            logger.warning(f"consent notice could not be queued: {type(e).__name__}")
            await self._failed("playback_error")
            return

        # Queued, not yet spoken: a later TTS failure is not seen here.
        self._notice_played = True
        version = consent_script_version()
        log_consent_event(
            "consent.notice_played",
            room_name=self._room_name,
            reason=version,
            workflow_run_id=self._workflow_run_id,
        )
        await self._persist(
            {
                "played_at": datetime.now(timezone.utc).isoformat(),
                "script_version": version,
            }
        )


def maybe_build_consent_gate(
    workflow_run, engine, is_realtime: bool = False
) -> Optional[RecordingConsentGate]:
    """A gate for every LIVEKIT inbound call; None leaves behavior unchanged."""
    from api.enums import WorkflowRunMode

    if not workflow_run or workflow_run.mode != WorkflowRunMode.LIVEKIT.value:
        return None
    context = workflow_run.initial_context or {}
    if context.get("direction") != "inbound":
        return None
    room_name = context.get("room_name") or ""
    return RecordingConsentGate(
        engine,
        room_name=room_name,
        workflow_run_id=workflow_run.id,
        is_realtime=is_realtime,
    )
