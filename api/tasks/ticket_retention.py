"""PDPA retention sweep for the built-in ticket store (S-L4-SCREENPOP, C7).

Daily cron: for every org still holding non-anonymized tickets, strip PII
(caller number, summary, notes) from tickets older than the org's
retention window. The row survives with `anonymized_at` set — that plus
the structured log line below is the audit trail. Orgs on an external
ticket server never accumulate rows here; their retention is the
wrapper's contract obligation.
"""

from loguru import logger

from api.db import db_client
from api.services.tickets.config import TICKET_MCP_CONFIG_KEY

# Applied when the org config doesn't set retention_days: C7 requires a
# bounded retention, so absence of config must not mean "keep forever".
DEFAULT_RETENTION_DAYS = 90


async def enforce_ticket_retention(_ctx) -> None:
    org_ids = await db_client.get_ticket_organization_ids()
    for org_id in org_ids:
        config = await db_client.get_configuration_value(
            org_id, TICKET_MCP_CONFIG_KEY, default={}
        )
        retention_days = (config or {}).get("retention_days", DEFAULT_RETENTION_DAYS)
        try:
            count = await db_client.anonymize_expired_tickets(org_id, retention_days)
        except Exception as e:
            logger.error(f"ticket retention sweep failed for org {org_id}: {e}")
            continue
        if count:
            logger.info(
                f"ticket_retention: anonymized {count} tickets "
                f"(org_id={org_id}, retention_days={retention_days})"
            )
