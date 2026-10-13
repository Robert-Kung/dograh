"""Boot-time probe of the restricted kb MCP tool (kb-service 3.3b, 3.5).

Modelled on ``tickets/startup_check.py``: probe, log loudly, **never block or
fail startup**. First deployment and restore-to-empty-database have no kb tool
yet (bootstrap creates it after dograh is up), and a kb config gap must not take
the whole AI reception down. The real block is the call-time comparison in
:mod:`api.services.kb_tool.runtime`.

Checks, in order:

1. ``KB_MCP_URL`` set (otherwise no ``mcp`` tool can ever match).
2. The canon's ``restricted_tool_types.mcp`` present, and its values equal the
   constants (drift report only -- the constants are the baseline).
3. Number of non-archived ``mcp`` tools in the database: 0 -> "not created
   yet"; >= 2 -> warn; each one compared field by field (definition and
   ExternalCredential type/header), mismatches named by field only.
4. A matching tool -> ``tools/list`` against the kb server
   (``KB_MCP_URL``, fixed timeouts, that tool's credential): warn when
   unreachable or when ``search_knowledge`` is not listed.

The URL and the credential never appear in a log line.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from api.db import db_client
from api.enums import ToolCategory
from api.services.kb_tool import fixed

_PREFIX = "kb.startup_check"


def _check_canon() -> list[str]:
    from api.services.platform_scope import (
        PlatformArtifactMissing,
        restricted_tool_types,
    )

    try:
        entry = restricted_tool_types().get("mcp")
    except PlatformArtifactMissing as exc:
        logger.error(f"{_PREFIX}: feature scope canon unreadable: {exc}")
        return ["canon"]
    if entry is None:
        logger.warning(
            f"{_PREFIX}: canon has no restricted_tool_types.mcp; the kb tool "
            f"will not be registered at call time"
        )
        return ["restricted_tool_types.mcp"]
    drift = fixed.canon_entry_mismatches(entry)
    if drift:
        logger.error(
            f"{_PREFIX}: canon restricted_tool_types.mcp disagrees with dograh's "
            f"fixed definition in fields {drift}; dograh enforces its own "
            f"constants -- fix the canon or the constants"
        )
    return drift


async def _credential_for(tool: Any):
    cfg_uuid = ((getattr(tool, "definition", None) or {}).get("config") or {}).get(
        "credential_uuid"
    )
    if not cfg_uuid:
        return None
    return await db_client.get_credential_by_uuid(cfg_uuid, tool.organization_id)


async def probe_kb_server(credential: Any) -> dict:
    """``tools/list`` against the kb server with the fixed values. Never raises."""
    from api.services.workflow.mcp_tool_session import McpToolSession

    url = fixed.expected_url()
    if url is None:
        return {"reachable": False, "has_search_knowledge": False}
    session = McpToolSession(
        tool_uuid="kb-startup-check",
        tool_name="kb",
        url=url,
        credential=credential,
        tools_filter=[],
        timeout_secs=fixed.KB_TIMEOUT_SECS,
        sse_read_timeout_secs=fixed.KB_SSE_READ_TIMEOUT_SECS,
    )
    try:
        await session.start()
        if not session.available:
            logger.error(
                f"{_PREFIX}: kb MCP server unreachable (KB_MCP_URL); FAQ lookups "
                f"will answer 'no result' until it is up"
            )
            return {"reachable": False, "has_search_knowledge": False}
        names = sorted(t["name"] for t in session.discovered_tools())
        has_tool = fixed.KB_SEARCH_TOOL in names
        if has_tool:
            logger.info(f"{_PREFIX}: kb MCP server lists {names!r}")
        else:
            logger.error(
                f"{_PREFIX}: kb MCP server does not list "
                f"'{fixed.KB_SEARCH_TOOL}' (lists {names!r}); FAQ lookups will "
                f"answer 'no result'"
            )
        return {"reachable": True, "has_search_knowledge": has_tool}
    finally:
        await session.close()


async def verify_kb_mcp_tool() -> dict:
    """Run every check and return a summary for tests. Never raises."""
    report: dict = {
        "url_configured": fixed.expected_url() is not None,
        "canon_drift": [],
        "count": None,
        "mismatches": {},
        "probe": None,
    }
    try:
        if not report["url_configured"]:
            logger.error(
                f"{_PREFIX}: {fixed.KB_MCP_URL_ENV} is not set; no mcp tool can "
                f"match the fixed kb definition, FAQ lookups are off"
            )
        report["canon_drift"] = _check_canon()

        tools = await db_client.get_non_archived_tools_by_category(
            ToolCategory.MCP.value
        )
        report["count"] = len(tools)
        if not tools:
            logger.warning(
                f"{_PREFIX}: no mcp tool in the database yet (first deployment or "
                f"restore); bootstrap creates the kb definition -- starting normally"
            )
            return report
        if len(tools) > fixed.KB_MAX_MCP_TOOLS:
            logger.error(
                f"{_PREFIX}: {len(tools)} mcp tools in the database, at most "
                f"{fixed.KB_MAX_MCP_TOOLS} allowed; only one matching the fixed "
                f"definition is ever opened per call "
                f"(uuids {[t.tool_uuid for t in tools]!r})"
            )

        matching = []
        for tool in tools:
            credential = await _credential_for(tool)
            fields = fixed.definition_mismatches(
                tool.definition
            ) + fixed.credential_mismatches(credential)
            report["mismatches"][tool.tool_uuid] = fields
            if fields:
                logger.error(
                    f"{_PREFIX}: mcp tool {tool.tool_uuid!r} differs from the "
                    f"fixed kb definition in fields {fields}; it will NOT be "
                    f"registered at call time"
                )
            else:
                matching.append(credential)

        if matching:
            report["probe"] = await probe_kb_server(matching[0])
    except Exception as e:  # noqa: BLE001 -- the probe never blocks startup
        logger.error(
            f"{_PREFIX}: check aborted ({type(e).__name__}); startup continues"
        )
    return report


_startup_task = None  # keep a reference so the task isn't GC'd mid-probe


def schedule_startup_check() -> None:
    """Fire the verification in the background so startup never waits on it."""
    global _startup_task
    _startup_task = asyncio.create_task(verify_kb_mcp_tool())
