"""L-0 server-side answering participant (answer-before-refer).

livekit-sip (v1.8 and main) keeps an inbound call at 180 until it subscribes to
an audio track *published by someone else in the room*, and a call that is not
answered cannot be REFERed (``can't transfer non established call``). The
engine-free exits — dispatch-failure safetynet, undispatched-room reconcile,
capacity overflow — have no agent in the room, so their "transfer to a human"
could never succeed and the caller got a 486. This participant joins the
``cs-`` room, publishes exactly one track and pushes a fixed prompt: livekit-sip
answers (task 0: 200 OK 4–7 ms after publish), the caller hears why, and the
caller of :func:`answering` then REFERs or ends the call.

Owned by L-0: no queue (L-B) player or prompts, no TTS, no provider
credentials. It never subscribes (C6/C7: no caller audio, nothing recorded) —
``auto_subscribe=False`` on top of a token without ``can_subscribe``.

**It does not end calls.** Leaving the room does not end the SIP leg (task 0.5:
livekit-sip sent no BYE within 100 s), so the room delete — which must happen
before this participant disconnects — belongs to the caller; see
``livekit_safetynet.answer_then_exit``.

Events (``answer.ok`` / ``answer.failed`` / ``answer.disabled`` /
``answer.saturated``) are emitted here, where the stage and timing are known,
so the two call sites cannot drift in what they report. Fields: room name,
the caller's reason, run id, stage, elapsed — never a number or SIP header.
"""

import asyncio
import os
import secrets
import time
import wave
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from loguru import logger

from api.services.observability import call_events
from api.services.pipecat.livekit_dispatcher import DEFAULT_ROOM_PREFIX

ANSWER_AUDIO_DIR_DEFAULT = "/srv/data/audio-platform"
PROMPT_FILES = {"transfer": "answer_transfer.wav", "end": "answer_end.wav"}

SAMPLE_RATE = 48000
NUM_CHANNELS = 1
SAMPLE_WIDTH = 2
FRAME_SAMPLES = 960  # 20 ms
FRAME_BYTES = FRAME_SAMPLES * NUM_CHANNELS * SAMPLE_WIDTH
# Played ahead of every prompt: the carrier may not have media up between the
# 200 OK and its ACK, and the prompt's first syllable is the one that says why.
LEAD_SILENCE_SECONDS = 0.3
QUEUE_SIZE_MS = 1000

# Stage limits (design D5, calibrated by task 0). connect covers one internal
# rtc retry: ~2/60 joins hit ``wait_pc_connection timed out`` and only got in
# at 15.1 s — another 15 s of ringback beats a 486.
CONNECT_TIMEOUT_SECONDS = 20.0
PUBLISH_TIMEOUT_SECONDS = 2.0
# Per play: lead silence + prompt + this slack (the AudioSource queue drains
# in real time after the last capture returns).
PLAY_SLACK_SECONDS = 1.0
DISCONNECT_TIMEOUT_SECONDS = 2.0
TOKEN_TTL_SECONDS = 60
# A prompt longer than this is a wrong file, not a longer message: every
# second is held in the concurrency slot and the overflow chain's bound.
MAX_PROMPT_SECONDS = 15.0

# All answering participants in this process (design D10): one PeerConnection
# each, sharing livekit-server's media ports with the agents (R-AV.11). 8 is
# the media load of ~2 AI calls. Not a setting until something says it must be.
MAX_CONCURRENT_ANSWERS = 8

IDENTITY_PREFIX = "agent-answer-"

ANSWER_OK_EVENT = "answer.ok"
ANSWER_FAILED_EVENT = "answer.failed"
ANSWER_DISABLED_EVENT = "answer.disabled"
ANSWER_SATURATED_EVENT = "answer.saturated"


class AnswerFailed(Exception):
    """The answering participant could not (or must no longer) play.

    ``stage``: ``room`` / ``connect`` / ``publish`` / ``play`` / ``deadline`` /
    ``caller_left`` (the SIP caller left) / ``disconnected`` (this participant
    lost the room — server-side kick, signal loss: an incident, not a hangup)
    — and, raised before anything joins, ``disabled`` (assets failed
    validation at boot) / ``saturated`` (concurrency limit).
    """

    def __init__(self, stage: str, detail: str = ""):
        super().__init__(f"{stage}: {detail}" if detail else stage)
        self.stage = stage


_prompts: dict[str, bytes] = {}
_disabled_reason: Optional[str] = "not_validated"
_active = 0


def answer_audio_dir() -> Path:
    return Path(os.environ.get("ANSWER_AUDIO_DIR") or ANSWER_AUDIO_DIR_DEFAULT)


def _prompt_seconds(pcm: bytes) -> float:
    return len(pcm) / (SAMPLE_RATE * NUM_CHANNELS * SAMPLE_WIDTH)


def _load_prompt(path: Path) -> bytes:
    with wave.open(str(path)) as w:
        shape = (w.getframerate(), w.getnchannels(), w.getsampwidth())
        if shape != (SAMPLE_RATE, NUM_CHANNELS, SAMPLE_WIDTH):
            raise ValueError(
                f"format {shape}, need {(SAMPLE_RATE, NUM_CHANNELS, SAMPLE_WIDTH)}"
            )
        pcm = w.readframes(w.getnframes())
    if not pcm:
        raise ValueError("empty")
    if _prompt_seconds(pcm) > MAX_PROMPT_SECONDS:
        raise ValueError(f"longer than {MAX_PROMPT_SECONDS:.0f} s")
    lead = b"\x00" * (
        int(LEAD_SILENCE_SECONDS * SAMPLE_RATE) * NUM_CHANNELS * SAMPLE_WIDTH
    )
    pcm = lead + pcm
    return pcm + b"\x00" * (-len(pcm) % FRAME_BYTES)


def validate_answer_assets() -> None:
    """Load and cache the two prompts at boot; never raises (design D4).

    Only with LiveKit configured. A failure disables answering and pages
    (``answer.disabled``) instead of refusing to boot: a refusal would take
    every AI call, the webhooks and the reconciler down to protect the few
    calls that fall back to a 486 without it. Preflight §9b blocks the deploy;
    this catches mount drift after it. The event carries the file name and
    the failure's type only — no paths, no exception text.
    """
    global _prompts, _disabled_reason
    if not os.environ.get("LIVEKIT_URL"):
        return
    directory = answer_audio_dir()
    loaded: dict[str, bytes] = {}
    for name, filename in PROMPT_FILES.items():
        try:
            loaded[name] = _load_prompt(directory / filename)
        except Exception as e:
            kind = "invalid" if isinstance(e, ValueError) else type(e).__name__
            _prompts, _disabled_reason = {}, f"{filename}: {kind}"
            logger.error(
                f"answering participant disabled: {filename}: {e} (dir {directory})"
            )
            call_events.emit(
                ANSWER_DISABLED_EVENT, room_name="", reason=_disabled_reason
            )
            return
    _prompts, _disabled_reason = loaded, None
    logger.info(
        f"answering participant ready: prompts "
        f"{ {k: round(_prompt_seconds(v), 2) for k, v in loaded.items()} } s, "
        f"hard cap {hard_cap_seconds():.1f} s"
    )


def _play_limit(pcm: bytes) -> float:
    return _prompt_seconds(pcm) + PLAY_SLACK_SECONDS


def hard_cap_seconds() -> float:
    """The participant's own lifetime bound (design D5): connect + publish +
    one play of each prompt + disconnect + source close. Excludes the
    caller's REFER (the concurrency slot is held through it, though)."""
    plays = sum(_play_limit(p) for p in _prompts.values())
    return (
        CONNECT_TIMEOUT_SECONDS
        + PUBLISH_TIMEOUT_SECONDS
        + plays
        + 2 * DISCONNECT_TIMEOUT_SECONDS
    )


class Answerer:
    """One answering participant. Built only inside :func:`answering`."""

    def __init__(self, *, room_name, reason, workflow_run_id):
        self._room_name = room_name
        self._reason = reason
        self._workflow_run_id = workflow_run_id
        self._started = time.monotonic()
        self._budget = hard_cap_seconds()
        self._source = None
        self._stop = asyncio.Event()
        self._stop_stage: Optional[str] = None
        self._lock = (
            asyncio.Lock()
        )  # one writer per AudioSource (InvalidState otherwise)

    def _elapsed_ms(self) -> int:
        return int((time.monotonic() - self._started) * 1000)

    def stop(self, stage: str) -> None:
        """``caller_left`` / ``disconnected``: the first one wins."""
        if self._stop_stage is None:
            self._stop_stage = stage
            self._stop.set()

    def failed(self, stage: str, detail: str = "") -> AnswerFailed:
        call_events.emit(
            ANSWER_FAILED_EVENT,
            room_name=self._room_name,
            reason=self._reason,
            workflow_run_id=self._workflow_run_id,
            elapsed_ms=self._elapsed_ms(),
            stage=stage,
        )
        return AnswerFailed(stage, detail)

    async def _bounded(self, aw, *, stage: str, limit: float):
        """Run ``aw`` within min(limit, remaining budget); a stop wins.

        On a timeout the work is cancelled and not awaited — a
        ``capture_frame`` that never returns must not hold the exit (AC9).
        """
        budget_binds = self._budget < limit
        task = asyncio.ensure_future(aw)
        stopped = asyncio.ensure_future(self._stop.wait())
        t0 = time.monotonic()
        try:
            done, _ = await asyncio.wait(
                {task, stopped},
                timeout=max(0.0, min(limit, self._budget)),
                return_when=asyncio.FIRST_COMPLETED,
            )
        except BaseException:
            task.cancel()
            raise
        finally:
            self._budget -= time.monotonic() - t0
            stopped.cancel()
        if stopped in done:
            # A stop wins even when the work also finished: _push stops
            # feeding on a stop and returns, which is not "played".
            task.cancel()
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            raise AnswerFailed(self._stop_stage)
        if task in done:
            return task.result()
        task.cancel()
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        raise AnswerFailed("deadline" if budget_binds else stage)

    async def play(self, prompt: str) -> None:
        """Push one prompt and return once it has played out.

        "Played" = the last frame was captured and the source's queue drained
        (``wait_for_playout``), so a REFER or room delete right after cannot
        cut the prompt. Raises :class:`AnswerFailed` (``play`` / ``deadline``
        / ``caller_left`` / ``disconnected``); the caller then deletes the room.
        """
        from livekit import rtc

        pcm = _prompts[prompt]
        async with self._lock:
            try:
                await self._bounded(
                    self._push(rtc, pcm), stage="play", limit=_play_limit(pcm)
                )
            except AnswerFailed as e:
                raise self.failed(e.stage) from e
            except Exception as e:
                raise self.failed("play", type(e).__name__) from e
        call_events.emit(
            ANSWER_OK_EVENT,
            room_name=self._room_name,
            reason=self._reason,
            workflow_run_id=self._workflow_run_id,
            elapsed_ms=self._elapsed_ms(),
            stage=prompt,
        )

    async def _push(self, rtc, pcm: bytes) -> None:
        for off in range(0, len(pcm), FRAME_BYTES):
            if self._stop.is_set():
                break  # _bounded reports the stop; stop feeding the queue
            frame = rtc.AudioFrame(
                pcm[off : off + FRAME_BYTES], SAMPLE_RATE, NUM_CHANNELS, FRAME_SAMPLES
            )
            await self._source.capture_frame(frame)
        await self._source.wait_for_playout()


@asynccontextmanager
async def answering(
    room_name: str,
    *,
    reason: str,
    workflow_run_id: Optional[int] = None,
    before_disconnect: Optional[Callable[[], Awaitable[None]]] = None,
):
    """Join ``room_name``, publish one track, yield an :class:`Answerer`.

    Raises :class:`AnswerFailed` before yielding (``room`` / ``disabled`` /
    ``saturated`` / ``connect`` / ``publish``). On every exit after the join
    attempt — failure, cancel or normal — ``before_disconnect`` runs first
    (the caller's room delete: once livekit-sip may have answered, the SIP
    leg must not be left alone in the room, review M1), then the participant
    disconnects within a bound and gives up past it. The rtc objects are
    built here, inside the running loop: built outside it, ``connect()``
    hangs forever with no error (queue media bot, 2026-07-21).
    """
    global _active
    ans = Answerer(room_name=room_name, reason=reason, workflow_run_id=workflow_run_id)

    if not room_name or not room_name.startswith(DEFAULT_ROOM_PREFIX):
        raise ans.failed("room")
    if _disabled_reason is not None:
        # Already paged once at boot (answer.disabled); per call only the log.
        logger.warning(
            f"answering disabled ({_disabled_reason}); {room_name} not answered"
        )
        raise AnswerFailed("disabled", _disabled_reason)
    if _active >= MAX_CONCURRENT_ANSWERS:
        call_events.emit(
            ANSWER_SATURATED_EVENT,
            room_name=room_name,
            reason=reason,
            workflow_run_id=workflow_run_id,
        )
        raise AnswerFailed("saturated")

    from livekit import rtc

    from api.services.pipecat.livekit_dispatcher import _sign_agent_token

    _active += 1
    room = None
    try:
        try:
            token = _sign_agent_token(
                room_name,
                f"{IDENTITY_PREFIX}{secrets.token_hex(4)}",
                can_subscribe=False,
                can_publish_data=False,
                can_publish_sources=["microphone"],
                ttl_seconds=TOKEN_TTL_SECONDS,
            )
            room = rtc.Room()
            sip_kind = rtc.ParticipantKind.PARTICIPANT_KIND_SIP
            # Registered before connecting, so a hangup during a slow join is
            # not missed (review M3).
            room.on(
                "participant_disconnected",
                lambda p: ans.stop("caller_left") if p.kind == sip_kind else None,
            )
            room.on("disconnected", lambda *_: ans.stop("disconnected"))
            await asyncio.wait_for(
                room.connect(
                    os.environ["LIVEKIT_URL"],
                    token,
                    # The native side gives up too; a cancelled Python wait
                    # alone would leave the FFI connect running (review L3).
                    options=rtc.RoomOptions(
                        auto_subscribe=False, connect_timeout=CONNECT_TIMEOUT_SECONDS
                    ),
                ),
                CONNECT_TIMEOUT_SECONDS,
            )
        except Exception as e:
            raise ans.failed("connect", type(e).__name__) from e
        try:
            ans._source = rtc.AudioSource(
                SAMPLE_RATE, NUM_CHANNELS, queue_size_ms=QUEUE_SIZE_MS
            )
            track = rtc.LocalAudioTrack.create_audio_track("answer", ans._source)
            await asyncio.wait_for(
                room.local_participant.publish_track(
                    track,
                    rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
                ),
                PUBLISH_TIMEOUT_SECONDS,
            )
        except Exception as e:
            raise ans.failed("publish", type(e).__name__) from e
        # The caller may have hung up before we joined: then there is no SIP
        # participant to answer, and no one to play to (review M3).
        if not any(p.kind == sip_kind for p in room.remote_participants.values()):
            ans.stop("caller_left")
        ans._budget -= time.monotonic() - ans._started
        yield ans
    finally:
        try:
            if room is not None:
                try:
                    if before_disconnect is not None:
                        await before_disconnect()
                finally:
                    try:
                        await asyncio.wait_for(
                            room.disconnect(), DISCONNECT_TIMEOUT_SECONDS
                        )
                    except Exception as e:
                        logger.warning(
                            f"answering disconnect abandoned for {room_name}: "
                            f"{type(e).__name__}"
                        )
                if ans._source is not None:
                    try:
                        await asyncio.wait_for(
                            ans._source.aclose(), DISCONNECT_TIMEOUT_SECONDS
                        )
                    except Exception as e:
                        logger.warning(
                            f"answering source close failed: {type(e).__name__}"
                        )
        finally:
            _active -= 1
