"""Database access for telephony phone numbers.

Phone numbers are first-class entities (PSTN, SIP URI, or SIP extension)
owned by a telephony configuration. They power both outbound caller-ID
selection and inbound call routing.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.future import select

from api.db.base_client import BaseDBClient
from api.db.models import (
    CcpTenantModel,
    TelephonyConfigurationModel,
    TelephonyPhoneNumberModel,
    WorkflowModel,
)
from api.utils.telephony_address import normalize_telephony_address

UNIQUE_DID_INDEX = "uq_phone_numbers_address_global"


class DidTakenError(Exception):
    """The number is already registered (by any organization). Carries no
    information about the holder."""


@dataclass(frozen=True)
class ResolvedDid:
    """LiveKit DID resolution (tenant-isolation〈來電號碼解析租戶〉)."""

    workflow_id: Optional[int]
    organization_id: int
    # ccp_tenants.status, or None when the number's organization is no tenant.
    tenant_status: Optional[str]
    mapped: bool


class TelephonyPhoneNumberClient(BaseDBClient):
    async def list_phone_numbers_for_config(
        self, telephony_configuration_id: int
    ) -> List[TelephonyPhoneNumberModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(TelephonyPhoneNumberModel)
                .where(
                    TelephonyPhoneNumberModel.telephony_configuration_id
                    == telephony_configuration_id
                )
                .order_by(TelephonyPhoneNumberModel.created_at)
            )
            return list(result.scalars().all())

    async def list_phone_numbers_with_workflow_name_for_config(
        self, telephony_configuration_id: int
    ) -> List[Tuple[TelephonyPhoneNumberModel, Optional[str]]]:
        """Same as :meth:`list_phone_numbers_for_config` but also returns the
        inbound workflow's display name (or None) for each row, fetched via a
        single LEFT JOIN so we don't load entire workflow rows."""
        async with self.async_session() as session:
            result = await session.execute(
                select(TelephonyPhoneNumberModel, WorkflowModel.name)
                .join(
                    WorkflowModel,
                    WorkflowModel.id == TelephonyPhoneNumberModel.inbound_workflow_id,
                    isouter=True,
                )
                .where(
                    TelephonyPhoneNumberModel.telephony_configuration_id
                    == telephony_configuration_id
                )
                .order_by(TelephonyPhoneNumberModel.created_at)
            )
            return [(row, name) for row, name in result.all()]

    async def list_active_normalized_addresses_for_config(
        self, telephony_configuration_id: int
    ) -> List[str]:
        """Active phone numbers as canonical address strings (E.164 for PSTN,
        normalized SIP otherwise) — the shape providers want in their
        ``from_numbers`` list for caller-ID and rate-limit pool keys."""
        async with self.async_session() as session:
            result = await session.execute(
                select(TelephonyPhoneNumberModel.address_normalized)
                .where(
                    TelephonyPhoneNumberModel.telephony_configuration_id
                    == telephony_configuration_id,
                    TelephonyPhoneNumberModel.is_active.is_(True),
                )
                .order_by(TelephonyPhoneNumberModel.created_at)
            )
            return [row[0] for row in result.all()]

    async def get_phone_number(
        self, phone_number_id: int
    ) -> Optional[TelephonyPhoneNumberModel]:
        async with self.async_session() as session:
            return await session.get(TelephonyPhoneNumberModel, phone_number_id)

    async def get_phone_number_for_config(
        self, phone_number_id: int, telephony_configuration_id: int
    ) -> Optional[TelephonyPhoneNumberModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(TelephonyPhoneNumberModel).where(
                    TelephonyPhoneNumberModel.id == phone_number_id,
                    TelephonyPhoneNumberModel.telephony_configuration_id
                    == telephony_configuration_id,
                )
            )
            return result.scalars().first()

    async def get_phone_number_with_provider_by_address(
        self, organization_id: int, address_normalized: str
    ) -> Optional[Tuple[TelephonyPhoneNumberModel, str]]:
        """The org's row for a canonical address — whichever config owns it —
        with that config's provider. (organization_id, address_normalized) is
        unique, so there is at most one."""
        async with self.async_session() as session:
            result = await session.execute(
                select(TelephonyPhoneNumberModel, TelephonyConfigurationModel.provider)
                .join(
                    TelephonyConfigurationModel,
                    TelephonyConfigurationModel.id
                    == TelephonyPhoneNumberModel.telephony_configuration_id,
                )
                .where(
                    TelephonyPhoneNumberModel.organization_id == organization_id,
                    TelephonyPhoneNumberModel.address_normalized == address_normalized,
                )
            )
            row = result.first()
            return (row[0], row[1]) if row else None

    async def find_inbound_workflow_for_did(
        self, canonical_did: str
    ) -> Optional[ResolvedDid]:
        """LiveKit inbound: resolve a canonical E.164 DID to its tenant.

        ``canonical_did`` is ``normalize_dialed_did`` output — the same
        normalization every DID write path uses. The organization comes from
        the number row (never from the workflow creator). ``mapped`` iff the
        row is active, has an inbound workflow in the same organization, and
        that organization is a CCP tenant; otherwise the row's organization is
        still returned (unmapped-but-owned). None only when no row holds the
        number. Never depends on the workflow's ``user_id``.
        """
        async with self.async_session() as session:
            row = (
                await session.execute(
                    select(
                        TelephonyPhoneNumberModel.organization_id,
                        TelephonyPhoneNumberModel.is_active,
                        TelephonyPhoneNumberModel.inbound_workflow_id,
                        WorkflowModel.organization_id.label("workflow_org"),
                        CcpTenantModel.status,
                    )
                    .outerjoin(
                        WorkflowModel,
                        WorkflowModel.id
                        == TelephonyPhoneNumberModel.inbound_workflow_id,
                    )
                    .outerjoin(
                        CcpTenantModel,
                        CcpTenantModel.organization_id
                        == TelephonyPhoneNumberModel.organization_id,
                    )
                    .where(
                        TelephonyPhoneNumberModel.address_normalized == canonical_did
                    )
                )
            ).one_or_none()
        if row is None:
            return None
        mapped = (
            row.is_active
            and row.inbound_workflow_id is not None
            and row.workflow_org == row.organization_id
            and row.status is not None
        )
        return ResolvedDid(
            workflow_id=row.inbound_workflow_id if mapped else None,
            organization_id=row.organization_id,
            tenant_status=row.status,
            mapped=bool(mapped),
        )

    async def find_active_phone_number_for_inbound(
        self,
        organization_id: int,
        address: str,
        provider: str,
        country_hint: Optional[str] = None,
    ) -> Optional[TelephonyPhoneNumberModel]:
        """Inbound routing primary lookup: normalize the called address and find
        the matching active row whose config is for the detected provider."""
        normalized = normalize_telephony_address(address, country_hint=country_hint)

        async with self.async_session() as session:
            result = await session.execute(
                select(TelephonyPhoneNumberModel)
                .join(
                    TelephonyConfigurationModel,
                    TelephonyConfigurationModel.id
                    == TelephonyPhoneNumberModel.telephony_configuration_id,
                )
                .where(
                    TelephonyPhoneNumberModel.organization_id == organization_id,
                    TelephonyPhoneNumberModel.address_normalized
                    == normalized.canonical,
                    TelephonyPhoneNumberModel.is_active.is_(True),
                    TelephonyConfigurationModel.provider == provider,
                )
            )
            return result.scalars().first()

    async def find_inbound_route_by_account(
        self,
        provider: str,
        account_id_field: str,
        account_id: str,
        to_number: str,
        country_hint: Optional[str] = None,
        organization_id: Optional[int] = None,
    ) -> Optional[Tuple[TelephonyConfigurationModel, TelephonyPhoneNumberModel]]:
        """Combined primary-path lookup for inbound dispatch.

        One SQL roundtrip that joins ``telephony_configurations`` and
        ``telephony_phone_numbers`` and matches all of:
        provider, ``credentials[account_id_field] == account_id``,
        ``phone.address_normalized == canonical(to_number)``, and
        ``phone.is_active``. Replaces the previous pattern of resolving the
        config and the phone number in two separate queries with a Python-side
        loop over candidate configs.

        Returns ``(config, phone_number)`` or None when the primary path
        misses (e.g. legacy non-E.164 stored addresses); the caller should
        fall back to the fuzzy ``numbers_match`` path in that case.
        """
        if not (provider and account_id_field and account_id and to_number):
            return None

        normalized = normalize_telephony_address(to_number, country_hint=country_hint)

        async with self.async_session() as session:
            stmt = (
                select(TelephonyConfigurationModel, TelephonyPhoneNumberModel)
                .join(
                    TelephonyPhoneNumberModel,
                    TelephonyPhoneNumberModel.telephony_configuration_id
                    == TelephonyConfigurationModel.id,
                )
                .where(
                    TelephonyConfigurationModel.provider == provider,
                    TelephonyConfigurationModel.credentials.op("->>")(account_id_field)
                    == account_id,
                    TelephonyPhoneNumberModel.address_normalized
                    == normalized.canonical,
                    TelephonyPhoneNumberModel.is_active.is_(True),
                )
            )
            if organization_id is not None:
                stmt = stmt.where(
                    TelephonyConfigurationModel.organization_id == organization_id
                )
            result = await session.execute(stmt)
            row = result.first()
            if not row:
                return None
            return row[0], row[1]

    async def find_inbound_routing_conflict(
        self,
        provider: str,
        account_id_field: str,
        account_id: str,
        address: str,
        country_hint: Optional[str] = None,
    ) -> Optional[Tuple[TelephonyConfigurationModel, TelephonyPhoneNumberModel]]:
        """Inbound dispatch keys on (provider, credentials[account_id_field],
        address_normalized) — see ``find_inbound_route_by_account``. That tuple
        must be globally unique or two orgs would race for the same call.

        Returns the conflicting (config, phone_number) — possibly in another
        org — when inserting a row with this combination would break that
        invariant, or None when the row is safe to insert. Returns None for
        providers that don't carry an account_id (e.g. ARI), which use a
        different inbound path.
        """
        if not (provider and account_id_field and account_id):
            return None

        normalized = normalize_telephony_address(address, country_hint=country_hint)

        async with self.async_session() as session:
            stmt = (
                select(TelephonyConfigurationModel, TelephonyPhoneNumberModel)
                .join(
                    TelephonyPhoneNumberModel,
                    TelephonyPhoneNumberModel.telephony_configuration_id
                    == TelephonyConfigurationModel.id,
                )
                .where(
                    TelephonyConfigurationModel.provider == provider,
                    TelephonyConfigurationModel.credentials.op("->>")(account_id_field)
                    == account_id,
                    TelephonyPhoneNumberModel.address_normalized
                    == normalized.canonical,
                )
            )
            result = await session.execute(stmt)
            row = result.first()
            return (row[0], row[1]) if row else None

    async def create_phone_number(
        self,
        organization_id: int,
        telephony_configuration_id: int,
        address: str,
        country_code: Optional[str] = None,
        label: Optional[str] = None,
        inbound_workflow_id: Optional[int] = None,
        is_active: bool = True,
        is_default_caller_id: bool = False,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> TelephonyPhoneNumberModel:
        normalized = normalize_telephony_address(address, country_hint=country_code)

        async with self.async_session() as session:
            # Platform-wide unique (mt-s1-foundation D3); the unique index is
            # the backstop for a concurrent registration.
            holder = (
                await session.execute(
                    select(TelephonyPhoneNumberModel.organization_id).where(
                        TelephonyPhoneNumberModel.address_normalized
                        == normalized.canonical
                    )
                )
            ).scalar_one_or_none()
            if holder is not None and holder != organization_id:
                raise DidTakenError()

            if is_default_caller_id:
                await self._clear_default_caller_id(session, telephony_configuration_id)

            row = TelephonyPhoneNumberModel(
                organization_id=organization_id,
                telephony_configuration_id=telephony_configuration_id,
                address=address,
                address_normalized=normalized.canonical,
                address_type=normalized.address_type,
                country_code=country_code or normalized.country_code,
                label=label,
                inbound_workflow_id=inbound_workflow_id,
                is_active=is_active,
                is_default_caller_id=is_default_caller_id,
                extra_metadata=extra_metadata or {},
            )
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as e:
                await session.rollback()
                if UNIQUE_DID_INDEX in str(e.orig):
                    # Lost a race with another organization's registration.
                    raise DidTakenError() from None
                raise e
            await session.refresh(row)
            return row

    async def update_phone_number(
        self,
        phone_number_id: int,
        telephony_configuration_id: int,
        label: Optional[str] = None,
        inbound_workflow_id: Optional[int] = None,
        is_active: Optional[bool] = None,
        country_code: Optional[str] = None,
        extra_metadata: Optional[Dict[str, Any]] = None,
        clear_inbound_workflow: bool = False,
    ) -> Optional[TelephonyPhoneNumberModel]:
        """Partial update. ``address`` is intentionally immutable — create a new
        row instead. Set ``clear_inbound_workflow=True`` to null out the FK."""
        async with self.async_session() as session:
            row = await session.get(TelephonyPhoneNumberModel, phone_number_id)
            if not row or row.telephony_configuration_id != telephony_configuration_id:
                return None

            if label is not None:
                row.label = label
            if inbound_workflow_id is not None:
                row.inbound_workflow_id = inbound_workflow_id
            elif clear_inbound_workflow:
                row.inbound_workflow_id = None
            if is_active is not None:
                row.is_active = is_active
            if country_code is not None:
                row.country_code = country_code
            if extra_metadata is not None:
                row.extra_metadata = extra_metadata

            await session.commit()
            await session.refresh(row)
            return row

    async def set_default_caller_id(
        self, phone_number_id: int, telephony_configuration_id: int
    ) -> Optional[TelephonyPhoneNumberModel]:
        async with self.async_session() as session:
            row = await session.get(TelephonyPhoneNumberModel, phone_number_id)
            if not row or row.telephony_configuration_id != telephony_configuration_id:
                return None
            await self._clear_default_caller_id(session, telephony_configuration_id)
            row.is_default_caller_id = True
            await session.commit()
            await session.refresh(row)
            return row

    async def get_default_caller_id(
        self, telephony_configuration_id: int
    ) -> Optional[TelephonyPhoneNumberModel]:
        async with self.async_session() as session:
            result = await session.execute(
                select(TelephonyPhoneNumberModel).where(
                    TelephonyPhoneNumberModel.telephony_configuration_id
                    == telephony_configuration_id,
                    TelephonyPhoneNumberModel.is_default_caller_id.is_(True),
                )
            )
            return result.scalars().first()

    async def delete_phone_number(
        self, phone_number_id: int, telephony_configuration_id: int
    ) -> bool:
        async with self.async_session() as session:
            row = await session.get(TelephonyPhoneNumberModel, phone_number_id)
            if not row or row.telephony_configuration_id != telephony_configuration_id:
                return False
            await session.delete(row)
            await session.commit()
            return True

    @staticmethod
    async def _clear_default_caller_id(
        session, telephony_configuration_id: int
    ) -> None:
        await session.execute(
            update(TelephonyPhoneNumberModel)
            .where(
                TelephonyPhoneNumberModel.telephony_configuration_id
                == telephony_configuration_id,
                TelephonyPhoneNumberModel.is_default_caller_id.is_(True),
            )
            .values(is_default_caller_id=False)
        )
