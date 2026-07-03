# Ticket MCP Contract — v1.0

The product interface between the Dograh customer-center platform and any
ticket/CRM backend. The platform's transfer handoff writes through these
MCP tools and nothing else; implement the REQUIRED tools in your own MCP
server (a thin wrapper over your CRM is typical) and the platform swaps
over with **zero agent-side changes** — that claim is mechanically checked
by the conformance runner below.

Machine-readable schemas live in `contract.py` (`TOOL_SCHEMAS`,
`ERROR_ENVELOPE_SCHEMA`, `CONTRACT_VERSION`) — that module is the source
of truth; this document explains it.

## Quickstart for wrapper engineers (target: under 2 hours)

1. Read this file, then `reference_server.py` — a complete, in-memory
   implementation of the contract in ~200 lines. It is the spec in code form.
2. Start it and see a green report:

   ```
   python -m api.services.tickets.reference_server --port 9100
   python -m api.services.tickets.conformance --url http://127.0.0.1:9100/mcp --token dev
   ```

3. Implement the two REQUIRED tools against your CRM, run the conformance
   CLI against *your* server until it is green. No platform account, SIP
   trunk, or database needed.
4. Hand your server URL + bearer token to the platform operator
   (`ticket_mcp_server` org configuration). Done.

## Tools

| Tool | Tier | Purpose |
|---|---|---|
| `create_ticket` | **REQUIRED** | Idempotent get-or-create of the handoff ticket at transfer time |
| `append_ticket_note` | **REQUIRED** | Append the summary / transfer_failed / generic notes (never mutates fields) |
| `get_ticket` | optional | Screen-pop lookup by ticket id |
| `find_tickets_by_caller` | optional | Screen-pop fallback lookup by E.164 caller number |

Missing optional tools are reported **once, loudly, at config load** and
the platform degrades its lookup path; per-call writes are unaffected.
Missing REQUIRED tools are a config-load **error**.

### Semantics you must honor

- **Idempotency** — `create_ticket` is get-or-create keyed on
  `workflow_run_id`. A timeout retry or double trigger must return the
  *existing* ticket (its `ticket_id` wins over the newly supplied one),
  never a second ticket.
- **Append, not update** — `append_ticket_note` adds to an append-only
  list. There is deliberately no field-mutation tool.
- **Anonymous callers** — `caller_number` may be `""`. That is a legal
  ticket; number-based lookup simply never finds it.
- **`verified_identity` is data you display, not truth you infer** — the
  platform sets it from deterministic verification state only. Readers
  must treat every other summary field as untrusted caller-derived
  narrative.
- **Unknown fields** — tolerate-and-ignore or reject the whole request;
  never store fields outside the contract.

## Error envelope

Failures are returned as the tool *result* (not protocol errors):

```json
{"error": {"code": "VALIDATION_FAILED", "message": "...", "retryable": false}}
```

Codes: `VALIDATION_FAILED` (bad input, don't retry) · `NOT_FOUND`
(unknown ticket — including tickets you refuse to reveal) · `UNAVAILABLE`
(transient backend failure, retry may help). The platform retries at most
once, and only when `retryable` is true.

## Latency budget

The skeleton write happens **off the caller's audio path** (fire-and-forget
after the REFER decision), but the human agent's screen-pop wants the
ticket queryable within seconds. Budget:

- `create_ticket` / `append_ticket_note`: respond in **< 1s** p50.
- If your CRM is slow, use **accept-then-enqueue**: validate, persist a
  stub, return the ticket view immediately, and complete the CRM write
  asynchronously. Do not hold the MCP response open on a slow upstream.
- The platform's per-call timeout is operator-configured
  (`timeout_seconds`, default 3s); a timeout counts as a failed write and
  is metric'd, never retried into your server more than once.

## Versioning

`contract_version` (currently `1.0`) is returned by every success shape.
Policy is **additive-only** within a major version: new optional fields
and new optional tools may appear; nothing is renamed, removed, or
re-typed. Both sides tolerate unknown fields. There is no version
negotiation — the platform checks your `tools/list` at config load and
logs drift.

## Data protection (PDPA / C7)

Tickets contain caller PII. Implementations MUST provide a bounded
retention story: either honor a retention window with deletion or
anonymization (the built-in server anonymizes in place after
`retention_days`, keeping an audit row), or document the equivalent
obligation your CRM already fulfills. Deletion/anonymization is part of
the contract's obligations now precisely because retrofitting it later
would be a breaking change.

## Correlation channels (how the ticket reaches the human)

1. **Primary**: the platform attaches the ticket id to the SIP REFER as
   `User-to-User` (standard UUI attached-data) and `X-Dograh-Ticket-Id`.
   Whether these survive to your ACD depends on the trunk — verify with
   your provider.
2. **Fallback**: E.164 caller-number lookup via `find_tickets_by_caller`
   (most-recent-first). This is why implementing the optional tools is
   strongly recommended.

The ticket id is deterministic (`CS-<workflow_run_id>`), so retries and
the REFER header always agree. `workflow_run_id` doubles as the
correlation id in platform logs — quote it when raising issues.
