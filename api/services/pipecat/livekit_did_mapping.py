"""Ensure a LiveKit DID -> AI workflow mapping (deployment bootstrap).

LiveKit inbound resolves the dialed DID through
``find_inbound_workflow_for_did`` against ``telephony_phone_numbers``, and
every phone-number row needs a ``telephony_configurations`` parent (FK). LiveKit
is not a telephony provider (C3): it is not in the provider registry nor in
``TelephonyConfigRequest``, so the regular config/number API cannot create
that parent. This module creates and reuses one ``provider='livekit'`` row per
org purely as the FK container for those numbers — empty credentials, never
the default outbound, no transport behind it. It is a stop-gap: the canonical
DID -> workflow store is S-L6-ROUTING.

Numbers are written directly; no provider ``configure_inbound`` sync runs,
since nothing upstream needs to be told about them.
"""

from dataclasses import dataclass
from typing import Literal, Optional

from sqlalchemy.exc import IntegrityError

from api.db import db_client
from api.services.pipecat.livekit_dispatcher import (
    DID_COUNTRY_HINT,
    normalize_dialed_did,
)
from api.utils.telephony_helper import get_country_code

LIVEKIT_PROVIDER = "livekit"
CONTAINER_CONFIG_NAME = "livekit"
AI_INBOUND_LABEL = "ai-inbound"

MappingStatus = Literal["created", "unchanged", "rebound", "conflict"]


class InvalidDidAddressError(ValueError):
    """The address does not normalize to a PSTN number."""


class WorkflowNotInOrgError(LookupError):
    """The workflow does not exist in the caller's organization."""


class DidMappingConflictError(Exception):
    """The mapping cannot be made without ambiguity or a concurrent write won."""


@dataclass(frozen=True)
class CurrentBinding:
    workflow_id: Optional[int]
    is_active: bool
    provider: str


@dataclass(frozen=True)
class DidMappingResult:
    status: MappingStatus
    config_id: int
    phone_number_id: int
    address_normalized: str
    workflow_id: int
    current: Optional[CurrentBinding] = None


async def ensure_livekit_did_mapping(
    organization_id: int, address: str, workflow_id: int, overwrite: bool = False
) -> DidMappingResult:
    """Make ``address`` route LiveKit inbound calls to ``workflow_id``.

    Idempotent: an already-correct mapping is reported ``unchanged`` with no
    write. A row bound elsewhere is only rebound with ``overwrite``, and a
    number owned by a real provider's config is never moved.
    """
    canonical = normalize_dialed_did(address)
    if canonical is None:
        raise InvalidDidAddressError(f"address {address!r} is not a PSTN number")

    workflow = await db_client.get_workflow(
        workflow_id, organization_id=organization_id
    )
    if not workflow:
        raise WorkflowNotInOrgError(f"workflow {workflow_id} not found")

    existing = await db_client.get_phone_number_with_provider_by_address(
        organization_id, canonical
    )
    if existing is not None:
        row, provider = existing
        if provider != LIVEKIT_PROVIDER:
            return _conflict(row, provider, canonical, workflow_id)

    container_id = await _resolve_container_config_id(organization_id)

    if existing is None:
        try:
            row = await db_client.create_phone_number(
                organization_id=organization_id,
                telephony_configuration_id=container_id,
                address=canonical,
                country_code=_country_code(canonical),
                label=AI_INBOUND_LABEL,
                inbound_workflow_id=workflow_id,
                is_active=True,
            )
        except IntegrityError as e:
            raise DidMappingConflictError(
                f"address {canonical} was registered concurrently"
            ) from e
        return _result("created", row, canonical, workflow_id)

    row, provider = existing
    if (
        row.telephony_configuration_id == container_id
        and row.inbound_workflow_id == workflow_id
        and row.is_active
    ):
        return _result("unchanged", row, canonical, workflow_id)

    if not overwrite:
        return _conflict(row, provider, canonical, workflow_id)

    row = await db_client.update_phone_number(
        row.id,
        container_id,
        inbound_workflow_id=workflow_id,
        is_active=True,
    )
    return _result("rebound", row, canonical, workflow_id)


async def _resolve_container_config_id(organization_id: int) -> int:
    configs = await db_client.list_telephony_configurations(organization_id)
    livekit = [c for c in configs if c.provider == LIVEKIT_PROVIDER]
    if len(livekit) > 1:
        raise DidMappingConflictError(
            f"organization has {len(livekit)} livekit telephony configurations; "
            "expected at most one"
        )
    if livekit:
        return livekit[0].id

    if any(c.name == CONTAINER_CONFIG_NAME for c in configs):
        raise DidMappingConflictError(
            f"telephony configuration name {CONTAINER_CONFIG_NAME!r} is taken "
            "by a non-livekit configuration"
        )
    try:
        config = await db_client.create_telephony_configuration(
            organization_id=organization_id,
            name=CONTAINER_CONFIG_NAME,
            provider=LIVEKIT_PROVIDER,
            credentials={},
            never_default_outbound=True,
        )
    except IntegrityError as e:
        raise DidMappingConflictError(
            f"telephony configuration {CONTAINER_CONFIG_NAME!r} was created "
            "concurrently"
        ) from e
    return config.id


def _country_code(canonical: str) -> Optional[str]:
    # create_phone_number re-normalizes with country_code as the hint; only a
    # TW number may carry it, or "+1555…" would be re-prefixed with 886.
    dial = get_country_code(DID_COUNTRY_HINT)
    return DID_COUNTRY_HINT if canonical[1:].startswith(dial) else None


def _result(status: MappingStatus, row, canonical: str, workflow_id: int):
    return DidMappingResult(
        status=status,
        config_id=row.telephony_configuration_id,
        phone_number_id=row.id,
        address_normalized=canonical,
        workflow_id=workflow_id,
    )


def _conflict(row, provider: str, canonical: str, workflow_id: int):
    return DidMappingResult(
        status="conflict",
        config_id=row.telephony_configuration_id,
        phone_number_id=row.id,
        address_normalized=canonical,
        workflow_id=workflow_id,
        current=CurrentBinding(
            workflow_id=row.inbound_workflow_id,
            is_active=row.is_active,
            provider=provider,
        ),
    )
