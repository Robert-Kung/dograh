"""Workflow-level ``transfer_call`` tool config lookup (shared).

The transfer gate's inputs — business-hours schedule and queue-health keys —
live in the workflow's ``transfer_call`` tool config. Two consumers need it:
the in-call engine (voice tool / press-0, via
``PipecatEngine.resolve_transfer_call_config``) and the engine-less capacity
overflow chain (S-L9-SCALE), which has a resolved workflow but no run. One
lookup so the two can never disagree on where the gate config comes from.
"""

from api.db import db_client
from api.enums import ToolCategory


async def find_transfer_call_config(workflow, organization_id: int) -> dict | None:
    """Return the workflow's ``transfer_call`` tool config, or None if absent.

    Scans every node's tools (a press-0 safety net is global, so the target is
    workflow-wide, not per-node) and returns the first ``transfer_call`` tool's
    ``config``.
    """
    tool_uuids: set[str] = set()
    for node in workflow.nodes.values():
        for tu in getattr(node, "tool_uuids", None) or []:
            tool_uuids.add(tu)
    if not tool_uuids:
        return None

    tools = await db_client.get_tools_by_uuids(list(tool_uuids), organization_id)
    for tool in tools:
        if tool.category == ToolCategory.TRANSFER_CALL.value:
            return (tool.definition or {}).get("config", {})
    return None
