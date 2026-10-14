"""Restricted MCP client tool: the one fixed kb ``search_knowledge`` definition.

``mcp`` (dograh as an MCP *client*) is not on the platform enabled set. The
canon instead lists it under ``restricted_tool_types``: the type is admitted at
call time only for a tool row that equals the fixed definition in
:mod:`api.services.kb_tool.fixed`. The baseline of that comparison is program
constants plus the deployment environment (``KB_MCP_URL``) -- **never** the
database row being checked (kb-service D4 "固定定義的基準").

- :mod:`.fixed` -- the constants and pure comparisons (no I/O).
- :mod:`.startup_check` -- boot-time probe: warns, never blocks (3.3b, 3.5).
- :mod:`.runtime` -- call-time admission, the "treat failure as no result"
  handler and the unavailable stub (3.3c, 3.5).
"""
