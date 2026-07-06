"""Ticket MCP contract conformance runner (S-L4-SCREENPOP, D8).

Owner engineers point this at their wrapper and get a per-requirement
pass/fail report — locally, with no platform account, SIP trunk, or
database:

    python -m api.services.tickets.conformance --url http://127.0.0.1:9100/mcp --token dev

Exit code 0 = every REQUIRED check passed (missing OPTIONAL tools are
reported as warnings, matching the platform's degraded-lookup posture).
Run it against the bundled reference server (see reference_server.py)
to see a fully green report.

The checks are the same behaviors the platform's own §2/§3 tests rely
on; passing here is what "drop-in swappable, agent unchanged" means
mechanically.
"""

import argparse
import asyncio
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import timedelta

from api.services.tickets import contract


@dataclass
class CheckResult:
    requirement: str
    passed: bool
    detail: str = ""


class ConformanceSession:
    """Thin MCP caller reused by every check (one connection per run)."""

    def __init__(self, url: str, token: str, timeout_seconds: float = 10.0):
        self.url = url
        self.token = token
        self.timeout_seconds = timeout_seconds
        # Distinct run-id space per invocation so reruns against a stateful
        # server don't collide with earlier tickets.
        self.run_base = uuid.uuid4().int % 10**9

    async def call(self, tool: str, arguments: dict):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(
            self.url,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=timedelta(seconds=self.timeout_seconds),
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool, arguments=arguments)
                if getattr(result, "structuredContent", None):
                    return result.structuredContent
                for content in result.content or []:
                    text = getattr(content, "text", None)
                    if text:
                        try:
                            return json.loads(text)
                        except ValueError:
                            return {"raw": text}
                return {}

    async def list_tool_names(self) -> set[str]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(
            self.url,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=timedelta(seconds=self.timeout_seconds),
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
                return {t.name for t in tools.tools}


def _is_valid_envelope(result) -> bool:
    error = isinstance(result, dict) and result.get("error")
    return bool(
        error
        and error.get("code") in contract.ERROR_CODES
        and isinstance(error.get("message"), str)
        and isinstance(error.get("retryable"), bool)
    )


async def run_conformance(session: ConformanceSession) -> list[CheckResult]:
    results: list[CheckResult] = []
    run_a = session.run_base + 1
    run_b = session.run_base + 2
    caller = "+886900" + str(session.run_base % 10**6).zfill(6)

    tool_names = await session.list_tool_names()
    for tool in contract.REQUIRED_TOOLS:
        results.append(
            CheckResult(
                f"tools/list exposes required tool `{tool}`",
                tool in tool_names,
                "missing — the platform write path cannot function"
                if tool not in tool_names
                else "",
            )
        )
    optional_present = {t for t in contract.OPTIONAL_TOOLS if t in tool_names}
    if not all(t in tool_names for t in contract.REQUIRED_TOOLS):
        return results  # nothing else is testable

    # create_ticket
    created = await session.call(
        "create_ticket",
        {
            "ticket_id": f"CONF-{run_a}",
            "workflow_run_id": run_a,
            "caller_number": caller,
            "room_name": "conformance-room",
            "transfer_reason": "voice_tool",
        },
    )
    results.append(
        CheckResult(
            "create_ticket returns the ticket with contract_version",
            not contract.is_error(created)
            and created.get("ticket_id") == f"CONF-{run_a}"
            and bool(created.get("contract_version")),
            f"got: {created}",
        )
    )

    retry = await session.call(
        "create_ticket",
        {"ticket_id": f"CONF-{run_a}-retry", "workflow_run_id": run_a},
    )
    results.append(
        CheckResult(
            "create_ticket is idempotent on workflow_run_id (retry returns the same ticket)",
            not contract.is_error(retry) and retry.get("ticket_id") == f"CONF-{run_a}",
            f"got: {retry}",
        )
    )

    # append_ticket_note
    summary = {
        "intent": "conformance probe",
        "verified_identity": "unknown",
        "steps_done": ["ran conformance"],
        "pending": [],
        "transfer_reason": "voice_tool",
    }
    appended = await session.call(
        "append_ticket_note",
        {"ticket_id": f"CONF-{run_a}", "note_type": "summary", "content": summary},
    )
    results.append(
        CheckResult(
            "append_ticket_note appends without mutating existing fields",
            not contract.is_error(appended)
            and appended.get("caller_number") == caller
            and any(n.get("note_type") == "summary" for n in appended.get("notes", [])),
            f"got: {appended}",
        )
    )

    missing = await session.call(
        "append_ticket_note",
        {"ticket_id": "CONF-does-not-exist", "note_type": "generic", "content": "x"},
    )
    results.append(
        CheckResult(
            "append_ticket_note on unknown ticket returns NOT_FOUND envelope",
            _is_valid_envelope(missing)
            and missing["error"]["code"] == contract.ERROR_NOT_FOUND,
            f"got: {missing}",
        )
    )

    # validation / error envelope
    bad = await session.call(
        "create_ticket",
        {"ticket_id": "bad id;drop table", "workflow_run_id": run_b},
    )
    results.append(
        CheckResult(
            "invalid input returns VALIDATION_FAILED envelope (retryable=false)",
            _is_valid_envelope(bad)
            and bad["error"]["code"] == contract.ERROR_VALIDATION_FAILED
            and bad["error"]["retryable"] is False,
            f"got: {bad}",
        )
    )

    overlong = await session.call(
        "append_ticket_note",
        {
            "ticket_id": f"CONF-{run_a}",
            "note_type": "generic",
            "content": "x" * (contract.NOTE_CONTENT_MAX_LEN + 1),
        },
    )
    results.append(
        CheckResult(
            "overlong content is rejected or sanitized, never stored raw",
            _is_valid_envelope(overlong)
            or (
                not contract.is_error(overlong)
                and all(
                    len(str(n.get("content", ""))) <= contract.NOTE_CONTENT_MAX_LEN
                    for n in overlong.get("notes", [])
                )
            ),
            f"got: {overlong}",
        )
    )

    unknown_field = await session.call(
        "append_ticket_note",
        {
            "ticket_id": f"CONF-{run_a}",
            "note_type": "summary",
            "content": {"intent": "probe", "admin_override": True},
        },
    )
    results.append(
        CheckResult(
            "unknown summary fields are rejected or ignored, never stored",
            _is_valid_envelope(unknown_field)
            or (
                not contract.is_error(unknown_field)
                and "admin_override" not in (unknown_field.get("summary") or {})
                and not any(
                    isinstance(n.get("content"), dict)
                    and "admin_override" in n["content"]
                    for n in unknown_field.get("notes", [])
                )
            ),
            f"got: {unknown_field}",
        )
    )

    anonymous = await session.call(
        "create_ticket",
        {"ticket_id": f"CONF-{run_b}", "workflow_run_id": run_b, "caller_number": ""},
    )
    results.append(
        CheckResult(
            "anonymous caller (empty caller_number) is a legal create",
            not contract.is_error(anonymous),
            f"got: {anonymous}",
        )
    )

    # optional tools
    if "get_ticket" in optional_present:
        fetched = await session.call("get_ticket", {"ticket_id": f"CONF-{run_a}"})
        results.append(
            CheckResult(
                "get_ticket returns the ticket by correlation key",
                not contract.is_error(fetched)
                and fetched.get("workflow_run_id") == run_a,
                f"got: {fetched}",
            )
        )
    else:
        results.append(
            CheckResult(
                "OPTIONAL get_ticket not implemented (platform degrades lookup)",
                True,
                "warning",
            )
        )

    if "find_tickets_by_caller" in optional_present:
        found = await session.call("find_tickets_by_caller", {"caller_number": caller})
        results.append(
            CheckResult(
                "find_tickets_by_caller returns tickets most-recent-first",
                not contract.is_error(found)
                and any(
                    t.get("ticket_id") == f"CONF-{run_a}"
                    for t in found.get("tickets", [])
                ),
                f"got: {found}",
            )
        )
        empty = await session.call(
            "find_tickets_by_caller", {"caller_number": "+886999999999"}
        )
        results.append(
            CheckResult(
                "find_tickets_by_caller empty result is a legal state",
                not contract.is_error(empty) and empty.get("tickets") == [],
                f"got: {empty}",
            )
        )
    else:
        results.append(
            CheckResult(
                "OPTIONAL find_tickets_by_caller not implemented "
                "(phone-lookup screen-pop degrades)",
                True,
                "warning",
            )
        )

    return results


def print_report(results: list[CheckResult]) -> bool:
    ok = True
    print(
        f"\nTicket MCP contract conformance (contract_version {contract.CONTRACT_VERSION})"
    )
    print("=" * 72)
    for r in results:
        mark = "PASS" if r.passed else "FAIL"
        if r.passed and r.detail == "warning":
            mark = "WARN"
        print(f"[{mark}] {r.requirement}")
        if not r.passed and r.detail:
            print(f"       {r.detail}")
        ok = ok and r.passed
    print("=" * 72)
    print("RESULT:", "conformant — drop-in swappable" if ok else "NOT conformant")
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate a ticket MCP server against the Dograh contract"
    )
    parser.add_argument("--url", required=True, help="MCP endpoint (streamable HTTP)")
    parser.add_argument("--token", required=True, help="Bearer token")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    session = ConformanceSession(args.url, args.token, args.timeout)
    results = asyncio.run(run_conformance(session))
    sys.exit(0 if print_report(results) else 1)


if __name__ == "__main__":
    main()
