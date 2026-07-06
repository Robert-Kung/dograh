"""Ticket server startup verification (S-L4-SCREENPOP, D8 loud checks).

At config-load time — app startup, before any transfer happens — every org
with a `ticket_mcp_server` configuration gets its server probed: a
handshake (one full initialize + tools/list round-trip, logged) and a
tool-set comparison against the contract. A missing REQUIRED tool logs an
error, a missing OPTIONAL tool logs a warning with the documented
degradation; either way the operator learns now, not on the first
mid-call transfer that silently drops context.

The probe never blocks or fails startup: an unreachable server is itself
a loud log line, and the write path will keep degrading per C4.
"""

import asyncio

from loguru import logger

from api.db import db_client
from api.services.tickets import contract
from api.services.tickets.config import (
    TICKET_MCP_CONFIG_KEY,
    TicketServerConfig,
    resolve_ticket_server_config,
)


async def probe_ticket_server(config: TicketServerConfig, org_id) -> dict:
    """Handshake + tools/list contract comparison for one configured server.

    Returns {reachable, missing_required, missing_optional} so ops tooling
    and tests can assert on it; the logging here is the product behavior.
    """
    from datetime import timedelta

    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    try:
        async with streamablehttp_client(
            config.url,
            headers={"Authorization": f"Bearer {config.api_key}"},
            timeout=timedelta(seconds=config.timeout_seconds),
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                init = await session.initialize()
                tools = await session.list_tools()
    except Exception as e:
        logger.error(
            f"ticket server probe FAILED (org={org_id}, url={config.url}): {e!r}. "
            f"Transfers will proceed but context writes will fail (C4)."
        )
        return {
            "reachable": False,
            "missing_required": list(contract.REQUIRED_TOOLS),
            "missing_optional": list(contract.OPTIONAL_TOOLS),
        }

    # The recorded handshake — one full request/response for diagnostics.
    logger.info(
        f"ticket server handshake (org={org_id}, url={config.url}): "
        f"server={getattr(init.serverInfo, 'name', '?')} "
        f"protocol={init.protocolVersion} "
        f"tools={sorted(t.name for t in tools.tools)}"
    )

    names = {t.name for t in tools.tools}
    missing_required = [t for t in contract.REQUIRED_TOOLS if t not in names]
    missing_optional = [t for t in contract.OPTIONAL_TOOLS if t not in names]
    if missing_required:
        logger.error(
            f"ticket server (org={org_id}) is missing REQUIRED contract tools "
            f"{missing_required} — transfer handoff writes will fail on every call"
        )
    if missing_optional:
        logger.warning(
            f"ticket server (org={org_id}) is missing OPTIONAL tools "
            f"{missing_optional} — screen-pop lookup degrades "
            f"(writes are unaffected)"
        )
    return {
        "reachable": True,
        "missing_required": missing_required,
        "missing_optional": missing_optional,
    }


async def verify_configured_ticket_servers() -> None:
    """Probe every org's configured ticket server. Never raises."""
    try:
        configured = await db_client.get_all_configurations_by_key(
            TICKET_MCP_CONFIG_KEY
        )
    except Exception as e:
        logger.warning(f"ticket server startup check skipped (config read failed): {e}")
        return

    for entry in configured:
        org_id = entry["organization_id"]
        config = await resolve_ticket_server_config(org_id)
        if config is None:
            continue  # disabled or incomplete config — nothing to probe
        await probe_ticket_server(config, org_id)


_startup_task = None  # keep a reference so the task isn't GC'd mid-probe


def schedule_startup_check() -> None:
    """Fire the verification in the background so startup never waits on it."""
    global _startup_task
    _startup_task = asyncio.create_task(verify_configured_ticket_servers())
