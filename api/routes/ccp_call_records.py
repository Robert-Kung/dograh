"""Internal call records for the platform console (ccp W4c 設計 A).

Service-to-service only: a dedicated secret (not the devops secret) that only
queue holds; the editor gateway denies the paths. Thin wrapper — validation,
queries and the audio read live in api.services.ccp.call_records.
"""

import secrets
from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask

from api.services.ccp import call_records as cr
from api.services.ccp import call_scope as cs

router = APIRouter(prefix="/internal/call-records", tags=["internal"])

CALL_RECORDS_SECRET_HEADER = "X-Dograh-Call-Records-Secret"

Outcome = Literal[
    "ai_completed", "transferred", "transfer_failed", "system_error", "unrecorded"
]
RecordingStatus = Literal[
    "available", "expired", "notice_failed", "notice_disabled", "none"
]


def _require_secret(
    provided: Annotated[str | None, Header(alias=CALL_RECORDS_SECRET_HEADER)] = None,
) -> None:
    # Read at call time (tests patch it). As a dependency it runs before body
    # validation — no 422 detail for an unauthenticated caller.
    from api import constants

    configured = constants.DOGRAH_CALL_RECORDS_SECRET
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Call records secret is not configured",
        )
    # Bytes: compare_digest raises TypeError on non-ASCII str.
    if not provided or not secrets.compare_digest(
        provided.encode("utf-8"), configured.encode("utf-8")
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


def _invalid(code: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=code)


def _unavailable(exc: Exception, what: str) -> HTTPException:
    if isinstance(exc, cr.CallRecordsTimeout):
        logger.warning(f"call records {what}: statement timeout")
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="timeout")
    # Type only: a DB error message could quote row values.
    logger.error(f"call records {what} failed: {type(exc).__name__}")
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="unavailable")


def _run_id(raw: str) -> int:
    run_id = cr.parse_run_id(raw)
    if run_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return run_id


class CallQueryBody(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    date_from: date = Field(alias="from")
    date_to: date = Field(alias="to")
    timezone: str
    codes: list[str] = []
    outcome: str | None = None
    category: str | None = None
    uncategorized: bool = False
    caller: str | None = None
    sort: str = "started_at"
    order: str = "desc"
    page: int = 1


class CallRow(BaseModel):
    run_id: int
    started_at: datetime
    ai_seconds: float
    outcome: Outcome
    category: str | None
    recording_status: RecordingStatus
    caller_masked: str | None


class CallListResponse(BaseModel):
    total: int
    page: int
    pages: int
    page_size: int
    rows: list[CallRow]
    recording_enabled: bool
    caller_search: Literal["full", "last4_only"]
    caller_coverage_from: date | None
    key_mismatch: bool


class Segment(BaseModel):
    at: datetime | None
    offset_ms: int | None
    seekable: bool
    speaker: Literal["caller", "ai"]
    text: str
    source: Literal["ai_leg"]


class Extracted(BaseModel):
    key: str
    value: str | bool | int | float


class Retention(BaseModel):
    audio_days: int
    transcript: int | Literal["never"] | None


class CallDetailResponse(BaseModel):
    run_id: int
    started_at: datetime
    ai_seconds: float
    outcome: Outcome
    handed_off: bool
    category: str | None
    caller_masked: str | None
    did: str | None
    recording_status: RecordingStatus
    transcript_status: Literal["available", "expired", "none"]
    retention: Retention
    segments: list[Segment]
    segments_truncated: bool
    extracted: list[Extracted]
    extracted_truncated: bool


@router.post(
    "/query",
    response_model=CallListResponse,
    dependencies=[Depends(_require_secret)],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": CallQueryBody.model_json_schema(by_alias=True)
                }
            },
        }
    },
)
async def query_calls(request: Request) -> CallListResponse:
    # Parsed by hand so a malformed body is a fixed code, not pydantic's echo.
    try:
        body = CallQueryBody.model_validate(await request.json())
    except Exception:
        raise _invalid("query_invalid") from None
    try:
        query = cr.validate_query(
            date_from=body.date_from,
            date_to=body.date_to,
            timezone=body.timezone,
            codes=body.codes,
            outcome=body.outcome,
            category=body.category,
            uncategorized=body.uncategorized,
            caller=body.caller,
            sort=body.sort,
            order=body.order,
            page=body.page,
        )
    except cr.CallRecordsInvalid as exc:
        raise _invalid(str(exc)) from None
    try:
        result = await cr.query_calls(query, timezone=body.timezone)
    except Exception as exc:
        raise _unavailable(exc, "query") from None
    return CallListResponse.model_validate(result)


@router.get(
    "/{run_id}",
    response_model=CallDetailResponse,
    dependencies=[Depends(_require_secret)],
)
async def get_call(
    run_id: str, codes: Annotated[list[str], Query()] = []
) -> CallDetailResponse:
    rid = _run_id(run_id)
    try:
        codes_t = cs.validate_codes(codes)
    except cs.ScopeInvalid:
        raise _invalid("query_invalid") from None
    try:
        result = await cr.get_call(rid, codes_t)
    except Exception as exc:
        raise _unavailable(exc, "detail") from None
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return CallDetailResponse.model_validate(result)


@router.get("/{run_id}/audio", dependencies=[Depends(_require_secret)])
async def get_audio(
    run_id: str, range_header: Annotated[str | None, Header(alias="Range")] = None
) -> StreamingResponse:
    rid = _run_id(run_id)
    try:
        stream = await cr.open_audio(rid, range_header)
    except cr.RangeNotSatisfiable as exc:
        headers = {} if exc.size is None else {"Content-Range": f"bytes */{exc.size}"}
        raise HTTPException(
            status_code=status.HTTP_416_RANGE_NOT_SATISFIABLE,
            detail="range not satisfiable",
            headers=headers,
        ) from None
    except cr.AudioBusy:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="busy"
        ) from None
    except Exception as exc:
        raise _unavailable(exc, "audio") from None
    if stream is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(stream.length),
        "Cache-Control": "no-store",
    }
    if stream.status == 206:
        last = stream.offset + stream.length - 1
        headers["Content-Range"] = f"bytes {stream.offset}-{last}/{stream.size}"
    return StreamingResponse(
        stream.body(),
        status_code=stream.status,
        media_type="audio/wav",
        headers=headers,
        background=BackgroundTask(stream.release),
    )
