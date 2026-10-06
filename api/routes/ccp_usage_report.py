"""Internal usage-report aggregate for the platform console (ccp W4b).

Service-to-service only (devops secret; the editor gateway denies the path).
Thin wrapper — validation and the query live in api.services.ccp.usage_report.
"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from loguru import logger
from pydantic import BaseModel

from api.services.ccp import usage_report as ur

router = APIRouter(prefix="/internal", tags=["internal"])


class UsageSplit(BaseModel):
    calls: int
    ai_completed: int
    transferred: int


class UsageDay(UsageSplit):
    date: date


class UsageOutcomes(BaseModel):
    ai_completed: int
    transferred: int
    transfer_failed: int
    system_error: int
    unrecorded: int


class UsageReportResponse(BaseModel):
    calls: int
    ai_seconds: float
    outcomes: UsageOutcomes
    daily: list[UsageDay]
    categories: dict[str, UsageSplit]
    unclassified: UsageSplit


def _require_devops_secret(
    x_dograh_devops_secret: Annotated[
        str | None,
        Header(alias="X-Dograh-Devops-Secret"),
    ] = None,
) -> None:
    # Function-level import: main imports this module's router at top level.
    # As a dependency it runs before query validation — no 422 detail for an
    # unauthenticated caller.
    from api.constants import DOGRAH_DEVOPS_SECRET
    from api.routes.main import _verify_devops_secret

    _verify_devops_secret(DOGRAH_DEVOPS_SECRET, x_dograh_devops_secret)


@router.get(
    "/usage-report",
    response_model=UsageReportResponse,
    dependencies=[Depends(_require_devops_secret)],
)
async def usage_report(
    date_from: Annotated[date, Query(alias="from")],
    date_to: Annotated[date, Query(alias="to")],
    timezone: Annotated[str, Query()],
    codes: Annotated[list[str], Query()] = [],
) -> UsageReportResponse:
    try:
        query = ur.validate_query(date_from, date_to, timezone, codes)
    except ur.UsageReportInvalid as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid {exc}",
        ) from None
    try:
        report = await ur.build_usage_report(query)
    except ur.UsageReportTimeout:
        logger.warning("usage report: statement timeout")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="timeout"
        ) from None
    except Exception as exc:
        # Type only: a DB error message could quote row values.
        logger.error(f"usage report failed: {type(exc).__name__}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="unavailable"
        ) from None
    return UsageReportResponse.model_validate(report)
