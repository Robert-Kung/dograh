"""The fixed kb MCP tool definition (kb-service D4) and pure comparisons.

Every value the restricted ``mcp`` tool may carry is declared here, once. The
platform repo's ``deploy/feature-scope.json`` repeats them under
``restricted_tool_types.mcp`` so the gateway, preflight and bootstrap can read
them; a pin test in the platform repo keeps the two copies equal. The call-time
and boot-time checks in this package compare against **these constants and the
deployment environment only** -- not against the canon's copy and not against
the database row (security r2 N3): a row that defines its own baseline can
never fail its own comparison.

Mismatch reports carry **field names only**. The URL and the credential are
never echoed (one is an internal address, the other a secret).
"""

from __future__ import annotations

import os
from typing import Any, Mapping, Optional

#: Deployment-layer env var carrying the kb MCP endpoint (reached over
#: ``platform_ai_kb``). Unset or blank -> no ``mcp`` tool matches.
KB_MCP_URL_ENV = "KB_MCP_URL"

#: The single raw MCP tool name the restricted definition exposes.
KB_SEARCH_TOOL = "search_knowledge"
KB_TOOLS_FILTER: tuple[str, ...] = (KB_SEARCH_TOOL,)

#: Connect timeout / read timeout (seconds). Pipeline hard ceiling for one
#: tool call = ``KB_SSE_READ_TIMEOUT_SECS + 5`` = 8 s (``McpToolSession``).
KB_TIMEOUT_SECS = 2
KB_SSE_READ_TIMEOUT_SECS = 3

#: ExternalCredential shape the kb tool must reference.
KB_CREDENTIAL_TYPE = "api_key"
KB_CREDENTIAL_HEADER = "X-KB-Token"

#: At most this many ``mcp``-category tools may exist in the database.
KB_MAX_MCP_TOOLS = 1

#: ``search_knowledge.query`` length cap; equals the kb CONTRACT value.
SEARCH_KNOWLEDGE_QUERY_MAX_LEN = 200

#: Handler-side deadline for one kb call. Shorter than the pipeline ceiling
#: (read timeout + 5) so that a hung call ends in *our* "no result" envelope
#: rather than in Pipecat's bare ``None`` timeout result.
KB_HANDLER_DEADLINE_SECS = KB_SSE_READ_TIMEOUT_SECS + 2

# Field names used in mismatch reports. Stable: tests and alert text use them.
FIELD_DEFINITION = "definition"
FIELD_URL = "url"
FIELD_TOOLS_FILTER = "tools_filter"
FIELD_CREDENTIAL_UUID = "credential_uuid"
FIELD_TIMEOUT = "timeout_secs"
FIELD_SSE_READ_TIMEOUT = "sse_read_timeout_secs"
FIELD_CREDENTIAL = "credential"
FIELD_CREDENTIAL_TYPE = "credential_type"
FIELD_CREDENTIAL_HEADER = "credential_header_name"


def expected_url() -> Optional[str]:
    """The kb MCP URL from the deployment environment, or None when unset."""
    value = (os.environ.get(KB_MCP_URL_ENV) or "").strip()
    return value or None


def definition_mismatches(definition: Any) -> list[str]:
    """Field names in which a tool ``definition`` departs from the fixed one.

    Empty list = the five fields equal the fixed definition. Pure apart from
    reading ``KB_MCP_URL``. An unparseable definition reports ``definition``.
    """
    from api.services.workflow.tools.mcp_tool import (
        McpDefinitionError,
        validate_mcp_definition,
    )

    try:
        cfg = validate_mcp_definition(definition)
    except McpDefinitionError:
        return [FIELD_DEFINITION]
    return config_mismatches(cfg)


def config_mismatches(cfg: Mapping[str, Any]) -> list[str]:
    """Same as :func:`definition_mismatches` for an already-validated config."""
    fields: list[str] = []
    url = expected_url()
    if url is None or cfg.get("url") != url:
        fields.append(FIELD_URL)
    if list(cfg.get("tools_filter") or []) != list(KB_TOOLS_FILTER):
        fields.append(FIELD_TOOLS_FILTER)
    if not cfg.get("credential_uuid"):
        fields.append(FIELD_CREDENTIAL_UUID)
    if cfg.get("timeout_secs") != KB_TIMEOUT_SECS:
        fields.append(FIELD_TIMEOUT)
    if cfg.get("sse_read_timeout_secs") != KB_SSE_READ_TIMEOUT_SECS:
        fields.append(FIELD_SSE_READ_TIMEOUT)
    return fields


def credential_mismatches(credential: Any) -> list[str]:
    """Field names in which the referenced ExternalCredential is wrong.

    ``None`` (missing, inactive, or in another organization) reports
    ``credential``. Never reads or returns the secret value.
    """
    if credential is None:
        return [FIELD_CREDENTIAL]
    fields: list[str] = []
    if getattr(credential, "credential_type", None) != KB_CREDENTIAL_TYPE:
        fields.append(FIELD_CREDENTIAL_TYPE)
    data = getattr(credential, "credential_data", None) or {}
    if not isinstance(data, dict) or data.get("header_name") != KB_CREDENTIAL_HEADER:
        fields.append(FIELD_CREDENTIAL_HEADER)
    return fields


def canon_entry_mismatches(entry: Any) -> list[str]:
    """Keys of the canon's ``restricted_tool_types.mcp`` that disagree with
    the constants here. Drift *report* only -- the canon is never the baseline.
    """
    if not isinstance(entry, dict):
        return ["restricted_tool_types.mcp"]
    expected = {
        "max_instances": KB_MAX_MCP_TOOLS,
        "url_env": KB_MCP_URL_ENV,
        "tools_filter": list(KB_TOOLS_FILTER),
        "timeout_secs": KB_TIMEOUT_SECS,
        "sse_read_timeout_secs": KB_SSE_READ_TIMEOUT_SECS,
        "credential": {"type": KB_CREDENTIAL_TYPE, "header_name": KB_CREDENTIAL_HEADER},
    }
    return [key for key, value in expected.items() if entry.get(key) != value]
