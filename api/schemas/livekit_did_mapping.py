"""Request/response schemas for ``POST /livekit/did-mappings``."""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class LiveKitDidMappingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: str = Field(..., min_length=1, max_length=255)
    workflow_id: int
    overwrite: bool = False


class LiveKitDidMappingCurrent(BaseModel):
    """The existing row's binding, returned on a conflict. Never carries the
    owning config's credentials."""

    workflow_id: Optional[int]
    is_active: bool
    provider: str


class LiveKitDidMappingResponse(BaseModel):
    status: Literal["created", "unchanged", "rebound", "conflict"]
    config_id: int
    phone_number_id: int
    address_normalized: str
    workflow_id: int
    current: Optional[LiveKitDidMappingCurrent] = None
