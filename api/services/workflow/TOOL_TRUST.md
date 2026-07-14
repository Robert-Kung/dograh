# Tool Trust Boundary (S-L8-TRUST)

On **LIVEKIT** calls the dialogue tool surface is deny-by-default: a tool
the LLM can trigger must have a trust spec declared in
[`tool_trust.py`](tool_trust.py), or it is hidden from the conversation —
its schema never reaches the LLM and no handler is registered. A skipped
tool logs a loud `trust.tool_denied` structured warning at registration.
Non-LIVEKIT modes (Twilio/ARI/webrtc/…) are completely unaffected.

**If your new tool silently "disappears" on LIVEKIT calls, this is why.**
Check the logs for `trust.tool_denied`, then declare a spec as below.

## Declaring a trust spec for a new tool

Policy lives in code (reviewable, versioned). Org/workflow config supplies
only *values* (destination numbers, field lists) — never rules.

1. **Pick the tier**
   - `read` — queries with no external side effect (`get_ticket`, KB retrieval)
   - `write` — creates/updates external state (`create_ticket`, HTTP tools)
   - `transfer` — moves the call (`transfer_call`)

2. **Declare where the tool lives**
   - Built-in category (END_CALL / TRANSFER_CALL / CALCULATOR / HTTP):
     add or reuse an entry in `FAMILY_TRUST`.
   - MCP tool: add an entry to `MCP_TOOL_TRUST` keyed by the **raw**
     server-side tool name (not the namespaced LLM name).

3. **Write the param rules**
   For every parameter the LLM may pass: `ParamRule(max_length=…,
   pattern=…, allowed_values=…)`. Undeclared params follow the tier-split
   policy: `read` gets global caps (length truncation + control-char
   strip), `write`/`transfer` are **rejected** with a structured
   `VALIDATION_FAILED` error the LLM can recover from (C4 — the call never
   goes silent).

4. **Bind identity params to platform truth**
   Params that identify the caller or run (`caller_number`,
   `workflow_run_id`) get `platform_bound={param: SOURCE_*}`. The guard
   overwrites whatever the LLM supplied with the platform value (SIP
   metadata / run id) — a hallucinated value never fails the call, but a
   mismatch emits `trust.override`.

5. **Add red-team cases**
   Extend `api/tests/test_tool_trust_redteam.py`: at least one attack case
   (out-of-contract param, oversized value) and one positive case proving
   the legitimate call still passes.

## Events

- `trust.violation` — guard rejected a call (or stripped undeclared
  transfer args). Windowed alert: repeated violations within the window
  (default 300 s / 3 hits, same knobs as `provider.error`) fire one
  summary to `OBS_ALERT_WEBHOOK_URL`.
- `trust.override` — a platform-bound param corrected the LLM's value.
  Logged, not alerted.

## Invariants the boundary enforces

- Transfer destinations come from workflow config (`transfer_to`) only;
  LLM-supplied numbers are stripped before the handler and can never
  reach the REFER (see `livekit-cold-transfer` spec).
- Caller-derived text reaches tools as *data*: `gathered_context` values
  are sanitized before template interpolation into HTTP preset params
  (`sanitize_untrusted`), and non-dialogue prompts (variable extraction,
  context summarization, handoff summary) fence transcript content as
  declared data.
- Advertised schemas must not carry handlers and catch-all
  `register_function(None, …)` is forbidden on LIVEKIT — both fail fast
  at context update (`trust.dormant_path`).
