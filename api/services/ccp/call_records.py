"""Per-call records for the platform console (ccp W4c 設計 A–H).

Three reads over the same counting scope as the usage report (``call_scope``):
a filtered, paged list; one call's detail with its transcript segments and
extracted values; and its mixed recording, read from MinIO by byte range.

Any run outside the scope — editor test calls, outbound, in progress, absent —
is "not found", indistinguishable from a missing id. Category codes leave the
database only if they are in the caller-supplied whitelist. Inputs are
rejected with fixed codes; nothing caller-supplied is echoed back.
"""

import asyncio
import json
import math
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from api.db import db_client
from api.services.ccp import call_scope as cs
from api.services.ccp import caller_identity as ci
from api.services.pipecat.livekit_consent import (
    audio_retention_days,
    consent_notice_text,
    transcript_retention,
)

PAGE_SIZE = 50
MAX_DAYS = 92
MAX_DAYS_WITH_CALLER = 366
MAX_RUN_ID = 2147483647
STATEMENT_TIMEOUT = "5s"
SORTS = ("started_at", "ai_seconds")
ORDERS = ("asc", "desc")

MAX_SEGMENTS = 2000
MAX_SEGMENT_CHARS = 4000
MAX_TRANSCRIPT_BYTES = 512 * 1024
MAX_EXTRACTED_KEYS = 50
MAX_EXTRACTED_CHARS = 500
MAX_EXTRACTED_DEPTH = 8
UNDISPLAYABLE = "（無法顯示）"

AUDIO_CONCURRENCY = 4
AUDIO_CHUNK = 64 * 1024

_RUN_ID = re.compile(r"[1-9]\d{0,9}")
_LAST4 = re.compile(r"\d{4}")
_RECORDING_KEY = re.compile(r"recordings/\d+\.wav")
_RANGE = re.compile(r"bytes=(\d*)-(\d*)")


class CallRecordsInvalid(ValueError):
    """Rejected input. ``str(exc)`` is a fixed code, never the value."""


class CallRecordsTimeout(Exception):
    """The statement timeout fired."""


class AudioBusy(Exception):
    """Every audio slot is in use."""


class RangeNotSatisfiable(Exception):
    def __init__(self, size: int | None):
        self.size = size


@dataclass(frozen=True)
class CallQuery:
    start: datetime
    end: datetime
    codes: tuple[str, ...]
    outcome: str | None
    category: str | None
    uncategorized: bool
    caller_hmac: str | None
    caller_last4: str | None
    caller_kind: str | None  # "full" | "last4"
    sort: str
    order: str
    page: int


def parse_run_id(raw: str) -> int | None:
    """1..2^31-1 as a decimal string without sign or leading zero, else None."""
    if not isinstance(raw, str) or not _RUN_ID.fullmatch(raw):
        return None
    value = int(raw)
    return value if value <= MAX_RUN_ID else None


def validate_query(
    *,
    date_from: date,
    date_to: date,
    timezone: str,
    codes: list[str],
    outcome: str | None = None,
    category: str | None = None,
    uncategorized: bool = False,
    caller: str | None = None,
    sort: str = "started_at",
    order: str = "desc",
    page: int = 1,
    now: datetime | None = None,
) -> CallQuery:
    caller_hmac = caller_last4 = caller_kind = None
    if caller is not None:
        if _LAST4.fullmatch(caller):
            caller_last4, caller_kind = caller, "last4"
        else:
            e164 = ci.normalize_caller(caller)
            if e164 is None:
                raise CallRecordsInvalid("caller_invalid")
            key = ci.hmac_key()
            if key is None:
                raise CallRecordsInvalid("caller_search_disabled")
            caller_hmac, caller_kind = ci.caller_hmac(key, e164), "full"
    try:
        cs.validate_period(
            date_from,
            date_to,
            timezone,
            max_days=MAX_DAYS_WITH_CALLER if caller_kind else MAX_DAYS,
            now=now,
        )
    except cs.ScopeInvalid:
        raise CallRecordsInvalid("period_invalid") from None
    try:
        codes_t = cs.validate_codes(codes)
    except cs.ScopeInvalid:
        raise CallRecordsInvalid("query_invalid") from None
    if outcome is not None and outcome not in cs.OUTCOME_CLASSES:
        raise CallRecordsInvalid("query_invalid")
    if category is not None and (uncategorized or category not in codes_t):
        raise CallRecordsInvalid("query_invalid")
    if sort not in SORTS or order not in ORDERS or not 1 <= page <= 10**6:
        raise CallRecordsInvalid("query_invalid")
    start, end = cs.period_bounds(date_from, date_to, timezone)
    return CallQuery(
        start=start,
        end=end,
        codes=codes_t,
        outcome=outcome,
        category=category,
        uncategorized=uncategorized,
        caller_hmac=caller_hmac,
        caller_last4=caller_last4,
        caller_kind=caller_kind,
        sort=sort,
        order=order,
        page=page,
    )


# --- shared SQL ---------------------------------------------------------------

_RECORDING_STATUS = """CASE
    WHEN wr.recording_url IS NOT NULL AND wr.recording_url <> '' THEN 'available'
    WHEN EXISTS (
        SELECT 1 FROM recording_retention_audit a
        WHERE a.workflow_run_id = wr.id AND a.result = 'ok'
          AND (a.scope = 'audio' OR (a.scope IS NULL AND EXISTS (
              SELECT 1 FROM json_array_elements_text(
                  CASE WHEN json_typeof(a.object_keys) = 'array'
                       THEN a.object_keys ELSE '[]'::json END) AS k
              WHERE starts_with(k, 'recordings/'))))
    ) THEN 'expired'
    WHEN json_typeof(wr.annotations->'consent_notice') = 'object'
         AND wr.annotations->'consent_notice'->>'failed_reason' IS NOT NULL
        THEN 'notice_failed'
    WHEN json_typeof(wr.annotations->'consent_notice') = 'object'
         AND wr.annotations->'consent_notice'->>'disabled' = 'true'
        THEN 'notice_disabled'
    ELSE 'none'
END"""

_HAS_SEGMENTS = """(json_typeof(wr.logs) = 'object'
    AND json_typeof(wr.logs->'realtime_feedback_events') = 'array'
    AND json_array_length(wr.logs->'realtime_feedback_events') > 0)"""

_TRANSCRIPT_STATUS = f"""CASE
    WHEN {_HAS_SEGMENTS} THEN 'available'
    WHEN EXISTS (
        SELECT 1 FROM recording_retention_audit a
        WHERE a.workflow_run_id = wr.id AND a.result = 'ok'
          AND (a.scope = 'transcript' OR (a.scope IS NULL AND EXISTS (
              SELECT 1 FROM json_array_elements_text(
                  CASE WHEN json_typeof(a.object_keys) = 'array'
                       THEN a.object_keys ELSE '[]'::json END) AS k
              WHERE starts_with(k, 'transcripts/'))))
    ) THEN 'expired'
    ELSE 'none'
END"""

_SCOPED = f"""
    SELECT
        wr.id,
        wr.created_at,
        {cs.seconds_expr("wr.")} AS secs,
        {cs.outcome_class_expr("(wr.annotations->>'call_outcome')")} AS cls,
        {cs.category_expr("wr.")} AS code,
        m.caller_masked,
        m.caller_hmac,
        m.caller_last4
    FROM workflow_runs wr
    LEFT JOIN ccp_call_meta m ON m.workflow_run_id = wr.id
    WHERE wr.created_at >= :start AND wr.created_at < :end
      AND {cs.scope_clause("wr.")}
"""

_FILTER = """
    WHERE (CAST(:outcome AS text) IS NULL OR cls = :outcome)
      AND (CAST(:category AS text) IS NULL OR code = :category)
      AND (NOT :uncategorized OR code IS NULL)
      AND (CAST(:caller_hmac AS text) IS NULL OR caller_hmac = :caller_hmac)
      AND (CAST(:caller_last4 AS text) IS NULL OR caller_last4 = :caller_last4)
"""

_COUNT_SQL = text(f"WITH scoped AS ({_SCOPED}) SELECT count(*) FROM scoped {_FILTER}")


def _page_sql(sort: str, order: str):
    # Both identifiers come from fixed tuples (validate_query), never input.
    col = {"started_at": "created_at", "ai_seconds": "secs"}[sort]
    direction = {"asc": "ASC", "desc": "DESC"}[order]
    return text(
        f"""WITH scoped AS ({_SCOPED})
        SELECT s.id, s.created_at, s.secs, s.cls, s.code, s.caller_masked,
               {_RECORDING_STATUS} AS recording_status
        FROM scoped s JOIN workflow_runs wr ON wr.id = s.id
        {_FILTER}
        ORDER BY s.{col} {direction}, s.id {direction}
        LIMIT :limit OFFSET :offset"""
    )


_META_SQL = text(
    """SELECT
        (SELECT min(created_at) FROM ccp_call_meta WHERE caller_last4 IS NOT NULL),
        (SELECT key_fingerprint FROM ccp_settings WHERE id = 1)"""
)


def _is_statement_timeout(exc: DBAPIError) -> bool:
    orig = exc.orig
    return (getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)) == "57014"


async def _read(fn):
    """Run ``fn(session)`` read-only under the statement timeout."""
    try:
        # The autobegun transaction carries SET LOCAL and is rolled back when
        # the session closes.
        async with db_client.async_session() as session:
            await session.execute(
                text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
            )
            return await fn(session)
    except DBAPIError as exc:
        if _is_statement_timeout(exc):
            raise CallRecordsTimeout() from None
        raise


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def _seconds(value) -> float:
    value = float(value or 0)
    return round(value, 3) if math.isfinite(value) else 0.0


# --- list -----------------------------------------------------------------------


async def query_calls(
    query: CallQuery, *, timezone: str, now: datetime | None = None
) -> dict:
    params = {
        "start": query.start,
        "end": query.end,
        "outcome": query.outcome,
        "category": query.category,
        "uncategorized": query.uncategorized,
        "caller_hmac": query.caller_hmac,
        "caller_last4": query.caller_last4,
        **cs.scope_params(now),
        **cs.category_params(query.codes),
        **cs.seconds_params(),
    }
    key = ci.hmac_key()

    async def run(session):
        total = int((await session.execute(_COUNT_SQL, params)).scalar_one())
        pages = max(1, math.ceil(total / PAGE_SIZE))
        page = min(query.page, pages)
        rows = (
            await session.execute(
                _page_sql(query.sort, query.order),
                {**params, "limit": PAGE_SIZE, "offset": (page - 1) * PAGE_SIZE},
            )
        ).all()
        coverage, fingerprint = (await session.execute(_META_SQL)).one()
        return total, pages, page, rows, coverage, fingerprint

    total, pages, page, rows, coverage, fingerprint = await _read(run)
    tz = ZoneInfo(timezone)
    return {
        "total": total,
        "page": page,
        "pages": pages,
        "page_size": PAGE_SIZE,
        "rows": [
            {
                "run_id": r.id,
                "started_at": _iso(r.created_at),
                "ai_seconds": _seconds(r.secs),
                "outcome": r.cls,
                "category": r.code,
                "recording_status": r.recording_status,
                "caller_masked": r.caller_masked,
            }
            for r in rows
        ],
        "recording_enabled": consent_notice_text() is not None,
        "caller_search": "full" if key else "last4_only",
        "caller_coverage_from": (
            coverage.astimezone(tz).date().isoformat() if coverage else None
        ),
        "key_mismatch": bool(
            key and fingerprint and fingerprint != ci.key_fingerprint(key)
        ),
    }


# --- detail ---------------------------------------------------------------------

_DETAIL_SQL = text(
    f"""SELECT
        wr.id,
        wr.created_at,
        {cs.seconds_expr("wr.")} AS secs,
        {cs.outcome_class_expr("(wr.annotations->>'call_outcome')")} AS cls,
        wr.annotations->>'call_outcome' AS outcome_raw,
        {cs.category_expr("wr.")} AS code,
        CASE WHEN json_typeof(wr.initial_context) = 'object'
             THEN wr.initial_context->>'did' END AS did,
        m.caller_masked,
        m.audio_started_at,
        {_RECORDING_STATUS} AS recording_status,
        {_TRANSCRIPT_STATUS} AS transcript_status,
        CASE WHEN json_typeof(wr.gathered_context) = 'object'
             THEN wr.gathered_context->'extracted_variables' END AS extracted
    FROM workflow_runs wr
    LEFT JOIN ccp_call_meta m ON m.workflow_run_id = wr.id
    WHERE wr.id = :run_id AND {cs.scope_clause("wr.")}"""
)

# Only the four fields a segment needs, filtered in SQL: the whole logs blob
# is never deserialised on the event loop (security M8).
_SEGMENTS_SQL = text(
    """SELECT e->>'type', e->>'timestamp', e->'payload'->>'timestamp',
              e->'payload'->>'text'
    FROM workflow_runs wr,
         json_array_elements(
             CASE WHEN json_typeof(wr.logs) = 'object'
                       AND json_typeof(wr.logs->'realtime_feedback_events') = 'array'
                  THEN wr.logs->'realtime_feedback_events' ELSE '[]'::json END
         ) WITH ORDINALITY AS t(e, n)
    WHERE wr.id = :run_id
      AND json_typeof(e) = 'object'
      AND (e->>'type' = 'rtf-bot-text'
           OR (e->>'type' = 'rtf-user-transcription'
               AND json_typeof(e->'payload') = 'object'
               AND e->'payload'->>'final' = 'true'))
    ORDER BY n
    LIMIT :limit"""
)

_SPEAKERS = {"rtf-user-transcription": "caller", "rtf-bot-text": "ai"}


def _parse_ts(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def build_segments(rows, audio_started_at: datetime | None) -> tuple[list, bool]:
    segments, truncated, total_bytes = [], len(rows) > MAX_SEGMENTS, 0
    for kind, ts, payload_ts, body in rows[:MAX_SEGMENTS]:
        if not isinstance(body, str) or not body.strip():
            continue
        if len(body) > MAX_SEGMENT_CHARS:
            body, truncated = body[:MAX_SEGMENT_CHARS], True
        size = len(body.encode())
        if total_bytes + size > MAX_TRANSCRIPT_BYTES:
            truncated = True
            break
        total_bytes += size
        at = _parse_ts(payload_ts) or _parse_ts(ts)
        offset_ms = None
        if at is not None and audio_started_at is not None:
            offset_ms = max(0, round((at - audio_started_at).total_seconds() * 1000))
        segments.append(
            {
                "at": _iso(at),
                "offset_ms": offset_ms,
                "seekable": offset_ms is not None,
                "speaker": _SPEAKERS[kind],
                "text": body,
                "source": "ai_leg",
            }
        )
    return segments, truncated


def _depth(value, level=0) -> int:
    if isinstance(value, dict):
        return max([_depth(v, level + 1) for v in value.values()] or [level + 1])
    if isinstance(value, list):
        return max([_depth(v, level + 1) for v in value] or [level + 1])
    return level


def _display(value) -> str | int | float | bool:
    if isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else UNDISPLAYABLE
    if isinstance(value, str):
        out = value
    else:
        try:
            if _depth(value) > MAX_EXTRACTED_DEPTH:
                return UNDISPLAYABLE
            out = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError, RecursionError):
            return UNDISPLAYABLE
    return out[:MAX_EXTRACTED_CHARS]


def build_extracted(raw) -> tuple[list, bool]:
    if not isinstance(raw, dict):
        return [], False
    items = list(raw.items())
    truncated = len(items) > MAX_EXTRACTED_KEYS
    out = []
    for key, value in items[:MAX_EXTRACTED_KEYS]:
        shown = _display(value)
        if isinstance(value, str) and len(value) > MAX_EXTRACTED_CHARS:
            truncated = True
        out.append({"key": str(key)[:MAX_EXTRACTED_CHARS], "value": shown})
    return out, truncated


async def get_call(
    run_id: int, codes: tuple[str, ...], *, now: datetime | None = None
) -> dict | None:
    params = {
        "run_id": run_id,
        **cs.scope_params(now),
        **cs.category_params(codes),
        **cs.seconds_params(),
    }

    async def run(session):
        row = (await session.execute(_DETAIL_SQL, params)).one_or_none()
        if row is None:
            return None, []
        segments = (
            await session.execute(
                _SEGMENTS_SQL, {"run_id": run_id, "limit": MAX_SEGMENTS + 1}
            )
        ).all()
        return row, segments

    row, segment_rows = await _read(run)
    if row is None:
        return None
    segments, truncated = build_segments(segment_rows, row.audio_started_at)
    extracted, extracted_truncated = build_extracted(row.extracted)
    transcript = transcript_retention()
    return {
        "run_id": row.id,
        "started_at": _iso(row.created_at),
        "ai_seconds": _seconds(row.secs),
        "outcome": row.cls,
        "handed_off": isinstance(row.outcome_raw, str)
        and row.outcome_raw.startswith("transferred:"),
        "category": row.code,
        "caller_masked": row.caller_masked,
        "did": row.did if isinstance(row.did, str) else None,
        "recording_status": row.recording_status,
        "transcript_status": row.transcript_status,
        "retention": {
            "audio_days": audio_retention_days(),
            "transcript": transcript,
        },
        "segments": segments,
        "segments_truncated": truncated,
        "extracted": extracted,
        "extracted_truncated": extracted_truncated,
    }


# --- audio ----------------------------------------------------------------------

_AUDIO_SQL = text(
    f"""SELECT wr.recording_url, wr.storage_backend
    FROM workflow_runs wr
    WHERE wr.id = :run_id AND {cs.scope_clause("wr.")}"""
)

_audio_slots = asyncio.Semaphore(AUDIO_CONCURRENCY)


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """``(offset, length)`` of one byte range, None for the whole file.

    Multiple ranges, other units or an unsatisfiable range raise
    :class:`RangeNotSatisfiable`.
    """
    if header is None:
        return None
    m = _RANGE.fullmatch(header.strip())
    if not m or (not m.group(1) and not m.group(2)):
        raise RangeNotSatisfiable(size)
    first, last = m.group(1), m.group(2)
    if first:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
        if start >= size or (last and int(last) < start):
            raise RangeNotSatisfiable(size)
    else:
        suffix = int(last)
        if suffix == 0 or size == 0:
            raise RangeNotSatisfiable(size)
        start, end = max(0, size - suffix), size - 1
    return start, end - start + 1


class AudioStream:
    """An open recording read. ``release`` is idempotent and MUST run once the
    response is over — the body's own ``finally`` covers a started stream, the
    route's background task one the client dropped before the first byte."""

    def __init__(self, status: int, size: int, offset: int, length: int, response):
        self.status, self.size, self.offset, self.length = status, size, offset, length
        self._response = response
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            self._response.close()
            self._response.release_conn()
        finally:
            _audio_slots.release()

    async def body(self) -> AsyncIterator[bytes]:
        try:
            while True:
                chunk = await asyncio.to_thread(self._response.read, AUDIO_CHUNK)
                if not chunk:
                    return
                yield chunk
        finally:
            self.release()


async def open_audio(
    run_id: int, range_header: str | None, *, now: datetime | None = None
) -> AudioStream | None:
    """The recording's bytes, or None when the call or its recording is absent.

    The DB session is closed before streaming starts — a slow listener never
    holds a connection the live calls share.
    """
    from api.services.filesystem.minio import MinioFileSystem
    from api.services.storage import get_storage_for_backend

    async def run(session):
        return (
            await session.execute(
                _AUDIO_SQL, {"run_id": run_id, **cs.scope_params(now)}
            )
        ).one_or_none()

    row = await _read(run)
    if row is None or not isinstance(row.recording_url, str):
        return None
    key = row.recording_url
    if not _RECORDING_KEY.fullmatch(key):
        return None
    try:
        fs = get_storage_for_backend(row.storage_backend)
    except ValueError:
        return None
    if not isinstance(fs, MinioFileSystem):
        return None

    if _audio_slots.locked():
        raise AudioBusy()
    await _audio_slots.acquire()
    try:
        meta = await fs.aget_file_metadata(key)
        if not meta or not isinstance(meta.get("size"), int):
            _audio_slots.release()
            return None
        size = meta["size"]
        span = parse_range(range_header, size)
        offset, length = span if span else (0, size)
        response = await asyncio.to_thread(
            fs.client.get_object, fs.bucket_name, key, offset, length if span else 0
        )
    except BaseException:
        _audio_slots.release()
        raise

    return AudioStream(206 if span else 200, size, offset, length, response)
