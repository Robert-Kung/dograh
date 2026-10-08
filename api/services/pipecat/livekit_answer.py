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
    ``caller_left`` — and, raised before anything joins, ``disabled`` (assets
    failed validation at boot) / ``saturated`` (concurrency limit).
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
    this catches mount drift after it.
    """
    global _prompts, _disabled_reason
    if not os.environ.get("LIVEKIT_URL"):
        return
    directory = answer_audio_dir()
    loaded: dict[str, bytes] = {}
    try:
        for name, filename in PROMPT_FILES.items():
            try:
                loaded[name] = _load_prompt(directory / filename)
            except Exception as e:
                raise ValueError(f"{filename}: {type(e).__name__}: {e}") from e
    except ValueError as e:
        _prompts, _disabled_reason = {}, str(e)
        logger.error(f"answering participant disabled: {e} (dir {directory})")
        call_events.emit(ANSWER_DISABLED_EVENT, room_name="", reason=str(e)[:200])
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
    one play of each prompt + disconnect. Excludes the caller's REFER."""
    plays = sum(_play_limit(p) for p in _prompts.values())
    return (
        CONNECT_TIMEOUT_SECONDS
        + PUBLISH_TIMEOUT_SECONDS
        + plays
        + DISCONNECT_TIMEOUT_SECONDS
    )


def disabled_reason() -> Optional[str]:
    return _disabled_reason


class Answerer:
    """One joined, published participant. Built only inside :func:`answering`."""

    def __init__(self, room, source, *, room_name, reason, workflow_run_id, started):
        self._room = room
        self._source = source
        self._room_name = room_name
        self._reason = reason
        self._workflow_run_id = workflow_run_id
        self._started = started
        self._budget = hard_cap_seconds() - (time.monotonic() - started)
        self._caller_left = asyncio.Event()
        self._lock = (
            asyncio.Lock()
        )  # one writer per AudioSource (InvalidState otherwise)

    def _elapsed_ms(self) -> int:
        return int((time.monotonic() - self._started) * 1000)

    def caller_left(self) -> None:
        self._caller_left.set()

    async def _bounded(self, aw, *, stage: str, limit: float):
        """Run ``aw`` within min(limit, remaining budget); caller hangup wins.

        On a timeout the work is cancelled and not awaited — a
        ``capture_frame`` that never returns must not hold the exit (AC9).
        """
        budget_binds = self._budget < limit
        task = asyncio.ensure_future(aw)
        left = asyncio.ensure_future(self._caller_left.wait())
        t0 = time.monotonic()
        try:
            done, _ = await asyncio.wait(
                {task, left},
                timeout=max(0.0, min(limit, self._budget)),
                return_when=asyncio.FIRST_COMPLETED,
            )
        except BaseException:
            task.cancel()
            raise
        finally:
            self._budget -= time.monotonic() - t0
            left.cancel()
        if task in done:
            return task.result()
        task.cancel()
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        if left in done:
            raise AnswerFailed("caller_left")
        raise AnswerFailed("deadline" if budget_binds else stage)

    async def play(self, prompt: str) -> None:
        """Push one prompt and return once it has played out.

        "Played" = the last frame was captured and the source's queue drained
        (``wait_for_playout``), so a REFER or room delete right after cannot
        cut the prompt. Raises :class:`AnswerFailed` (``play`` / ``deadline``
        / ``caller_left``); the caller then deletes the room.
        """
        from livekit import rtc

        pcm = _prompts[prompt]
        async with self._lock:
            try:
                await self._bounded(
                    self._push(rtc, pcm), stage="play", limit=_play_limit(pcm)
                )
            except AnswerFailed as e:
                self._failed(e.stage)
                raise
            except Exception as e:
                self._failed("play")
                raise AnswerFailed("play", type(e).__name__) from e
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
            if self._caller_left.is_set():
                break  # _bounded reports caller_left; stop feeding the queue
            frame = rtc.AudioFrame(
                pcm[off : off + FRAME_BYTES], SAMPLE_RATE, NUM_CHANNELS, FRAME_SAMPLES
            )
            await self._source.capture_frame(frame)
        await self._source.wait_for_playout()

    def _failed(self, stage: str) -> None:
        call_events.emit(
            ANSWER_FAILED_EVENT,
            room_name=self._room_name,
            reason=self._reason,
            workflow_run_id=self._workflow_run_id,
            elapsed_ms=self._elapsed_ms(),
            stage=stage,
        )


@asynccontextmanager
async def answering(
    room_name: str, *, reason: str, workflow_run_id: Optional[int] = None
):
    """Join ``room_name``, publish one track, yield an :class:`Answerer`.

    Raises :class:`AnswerFailed` before yielding (``room`` / ``disabled`` /
    ``saturated`` / ``connect`` / ``publish``). On exit — any exit, cancel
    included — disconnects within a bound and gives up past it; the room
    delete that actually ends the call is the caller's and MUST come first
    (``answer_then_exit``). The rtc objects are built here, inside the running
    loop: built outside it, ``connect()`` hangs forever with no error
    (queue media bot, 2026-07-21).
    """
    global _active
    started = time.monotonic()

    def _fail(stage: str, detail: str = "") -> AnswerFailed:
        call_events.emit(
            ANSWER_FAILED_EVENT,
            room_name=room_name,
            reason=reason,
            workflow_run_id=workflow_run_id,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            stage=stage,
        )
        return AnswerFailed(stage, detail)

    if not room_name or not room_name.startswith(DEFAULT_ROOM_PREFIX):
        raise _fail("room")
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
        source = None
        try:
            try:
                await asyncio.wait_for(
                    room.connect(
                        os.environ["LIVEKIT_URL"],
                        token,
                        options=rtc.RoomOptions(auto_subscribe=False),
                    ),
                    CONNECT_TIMEOUT_SECONDS,
                )
            except Exception as e:
                raise _fail("connect", type(e).__name__) from e
            source = rtc.AudioSource(
                SAMPLE_RATE, NUM_CHANNELS, queue_size_ms=QUEUE_SIZE_MS
            )
            track = rtc.LocalAudioTrack.create_audio_track("answer", source)
            try:
                await asyncio.wait_for(
                    room.local_participant.publish_track(
                        track,
                        rtc.TrackPublishOptions(
                            source=rtc.TrackSource.SOURCE_MICROPHONE
                        ),
                    ),
                    PUBLISH_TIMEOUT_SECONDS,
                )
            except Exception as e:
                raise _fail("publish", type(e).__name__) from e

            ans = Answerer(
                room,
                source,
                room_name=room_name,
                reason=reason,
                workflow_run_id=workflow_run_id,
                started=started,
            )
            sip_kind = rtc.ParticipantKind.PARTICIPANT_KIND_SIP
            room.on(
                "participant_disconnected",
                lambda p: ans.caller_left() if p.kind == sip_kind else None,
            )
            # Kicked by a room delete from elsewhere: nobody left to talk to.
            room.on("disconnected", lambda *_: ans.caller_left())
            yield ans
        finally:
            try:
                await asyncio.wait_for(room.disconnect(), DISCONNECT_TIMEOUT_SECONDS)
            except Exception as e:
                logger.warning(
                    f"answering disconnect abandoned for {room_name}: {type(e).__name__}"
                )
            if source is not None:
                try:
                    await asyncio.wait_for(source.aclose(), DISCONNECT_TIMEOUT_SECONDS)
                except Exception as e:
                    logger.warning(f"answering source close failed: {type(e).__name__}")
    finally:
        _active -= 1
