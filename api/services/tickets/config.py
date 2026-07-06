"""Ticket MCP server configuration resolution (S-L4-SCREENPOP, D5).

Org-level first (`organization_configurations` key "ticket_mcp_server"),
workflow-level override second (same key inside the run definition's
`workflow_configurations`, replacing the org value wholesale). No config
→ None → the entire handoff step is a no-op and cold transfer behaves
exactly as before this capability existed.

Value schema:
    {"enabled": bool, "url": str, "api_key": str,
     "timeout_seconds": float, "retention_days": int}

`api_key` is a per-org Dograh API key (bearer): the MCP server resolves
it to its owning organization, which is how credential-org == workflow-org
is enforced at write time (D3).
"""

from dataclasses import dataclass
from typing import Optional

from api.db import db_client

TICKET_MCP_CONFIG_KEY = "ticket_mcp_server"

DEFAULT_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True)
class TicketServerConfig:
    url: str
    api_key: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


async def resolve_ticket_server_config(
    organization_id: Optional[int],
    workflow_configurations: Optional[dict] = None,
) -> Optional[TicketServerConfig]:
    """Effective config for a run, or None when handoff is not configured."""
    raw = (workflow_configurations or {}).get(TICKET_MCP_CONFIG_KEY)
    if raw is None and organization_id is not None:
        raw = await db_client.get_configuration_value(
            organization_id, TICKET_MCP_CONFIG_KEY, default=None
        )
    if not raw or not raw.get("enabled"):
        return None
    url = (raw.get("url") or "").strip()
    api_key = (raw.get("api_key") or "").strip()
    if not url or not api_key:
        return None
    return TicketServerConfig(
        url=url,
        api_key=api_key,
        timeout_seconds=float(raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)),
    )
