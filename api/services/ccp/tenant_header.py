"""Organization carried on CCP service-to-service requests (mt-s1-foundation D7).

tenant-isolation〈服務間請求攜帶組織編號〉: callers send ``X-CCP-Organization-Id``
next to the shared service secret. The receiver checks the secret first, then
this: a malformed or missing value is 400 ``organization_required``; a
well-formed organization that is not a CCP tenant is 403 ``tenant_unknown``.
There is no fallback to "all organizations" or "the first tenant".
"""

from __future__ import annotations

import re

from api.services.ccp import tenants

ORGANIZATION_HEADER = "X-CCP-Organization-Id"

# 1..2147483647 (Postgres integer), decimal, no sign / leading zero / spaces.
_SHAPE = re.compile(r"[1-9][0-9]{0,9}")
_MAX = 2_147_483_647


class TenantHeaderError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


def parse_organization_id(raw: str | None) -> int | None:
    if raw is None or not _SHAPE.fullmatch(raw):
        return None
    value = int(raw)
    return value if value <= _MAX else None


async def resolve_tenant_organization(raw: str | None) -> int:
    """The tenant's organization id, or ``TenantHeaderError``."""
    organization_id = parse_organization_id(raw)
    if organization_id is None:
        raise TenantHeaderError(400, "organization_required")
    if not await tenants.is_tenant(organization_id):
        raise TenantHeaderError(403, "tenant_unknown")
    return organization_id
