"""Recording consent gate (S-L8-RECORD, PDPA).

dograh already records LIVEKIT calls (audio buffers → storage); this module
adds the compliance layer in front: play a recording notice before any
conversation, keep a consent record, and — fail-safe — produce **no recording
at all** when the notice was not configured or could not be played. The
notice audio itself lands at the head of the recording, self-evidencing.
Transcripts are unaffected (necessary service processing, not enhanced
collection).

Consent model is notice-based: the caller continuing after the notice is
consent. The consent record (``consent_notice`` in workflow_run annotations)
is compliance evidence and is never deleted with the recording.
"""

import os
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

_DEFAULT_SCRIPT_VERSION = "draft-0"
_DEFAULT_RETENTION_DAYS = 180


def consent_notice_text() -> Optional[str]:
    value = (os.environ.get("RECORD_CONSENT_NOTICE_TEXT") or "").strip()
    return value or None


def consent_script_version() -> str:
    return os.environ.get("RECORD_CONSENT_SCRIPT_VERSION", _DEFAULT_SCRIPT_VERSION)


def retention_days() -> int:
    return int(os.environ.get("RECORD_RETENTION_DAYS", _DEFAULT_RETENTION_DAYS))


def validate_recording_config() -> None:
    """Fail loudly at startup on malformed recording config."""
    try:
        days = retention_days()
    except ValueError as e:
        raise RuntimeError(f"RECORD_RETENTION_DAYS is not a number: {e}") from e
    if days <= 0:
        raise RuntimeError(f"RECORD_RETENTION_DAYS must be > 0, got {days}")


def log_consent_event(
    event: str,
    *,
    room_name: str,
    reason: str,
    workflow_run_id: Optional[int] = None,
) -> None:
    """Structured ``consent.*`` events (same field shape as safetynet/obs)."""
    logger.bind(
        call_event=event,
        room_name=room_name,
        reason=reason,
        workflow_run_id=workflow_run_id,
        elapsed_ms=None,
    ).warning(
        f"{event} room={room_name} reason={reason} workflow_run_id={workflow_run_id}"
    )


class RecordingConsentGate:
    """Plays the recording notice and gates recording on its outcome (C4).

    ``should_record`` stays False until the notice was successfully queued;
    a playback failure logs ``consent.notice_failed`` and the call proceeds
    unrecorded — never interrupted.
    """

    def __init__(self, engine, *, room_name: str, workflow_run_id: int):
        self._engine = engine
        self._room_name = room_name
        self._workflow_run_id = workflow_run_id
        self._notice_played = False

    @property
    def should_record(self) -> bool:
        return self._notice_played

    async def play_notice(self) -> None:
        text = consent_notice_text()
        if text is None:
            logger.info(
                f"RECORD_CONSENT_NOTICE_TEXT not set; call {self._workflow_run_id} "
                "proceeds without notice and without recording (fail-safe)"
            )
            return
        try:
            from pipecat.frames.frames import TTSSpeakFrame

            await self._engine.task.queue_frame(
                TTSSpeakFrame(text, persist_to_logs=True)
            )
        except Exception as e:
            log_consent_event(
                "consent.notice_failed",
                room_name=self._room_name,
                reason=str(e)[:200],
                workflow_run_id=self._workflow_run_id,
            )
            return

        self._notice_played = True
        version = consent_script_version()
        log_consent_event(
            "consent.notice_played",
            room_name=self._room_name,
            reason=version,
            workflow_run_id=self._workflow_run_id,
        )
        try:
            from api.db import db_client

            await db_client.update_workflow_run(
                self._workflow_run_id,
                annotations={
                    "consent_notice": {
                        "played_at": datetime.now(timezone.utc).isoformat(),
                        "script_version": version,
                    }
                },
            )
        except Exception as e:
            logger.error(f"failed to persist consent record: {e}")


def maybe_build_consent_gate(workflow_run, engine) -> Optional[RecordingConsentGate]:
    """A gate for every LIVEKIT inbound call; None leaves behavior unchanged."""
    from api.enums import WorkflowRunMode

    if not workflow_run or workflow_run.mode != WorkflowRunMode.LIVEKIT.value:
        return None
    context = workflow_run.initial_context or {}
    if context.get("direction") != "inbound":
        return None
    room_name = context.get("room_name") or ""
    return RecordingConsentGate(
        engine, room_name=room_name, workflow_run_id=workflow_run.id
    )
