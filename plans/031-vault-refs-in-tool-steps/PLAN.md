# 031 — tool_call steps never resolve `vault:<name>` refs

Status: OPEN (bug filed 2026-09-04, found via error-log-tracker digest playbook)

## Symptom

A playbook `tool_call` step passing a header like
`"x-api-key": "vault:luna_service_feedback_key"` to `http_request` gets
`401 Invalid API key` from luna-service. The same call made directly by the
agent (chat turn) with the identical vault ref returns 200.

## Root cause

Vault-ref substitution (luna 030, `luna/agent/vault_refs.py`) lives in the
agent runtime's tool-dispatch gate (`runtime.py` ~2154: `has_vault_refs` →
`resolve_vault_refs`, after the approval gate). The playbook runner bypasses
that gate entirely — `_run_tool_call` in `plugin_playbooks/runner.py` fetches
the handler from the registry and calls it directly:

    rt = self._tools.get(step.tool)
    result = await rt.handler(**args)

So the literal string `vault:luna_service_feedback_key` is sent as the header
value. The server sha256-hashes it, finds no matching key, and correctly
returns 401. Not a stale-credential or grant problem — refs are simply never
resolved on this path. `_run_code` (code steps) has the same gap.

## Fix sketch

In `_run_tool_call` (and `_run_code` inputs), after template rendering and
before `rt.handler(**args)`:

    from luna.agent.vault_refs import has_vault_refs, resolve_vault_refs
    if has_vault_refs(args):
        args, err = await resolve_vault_refs(args, plugin=rt.plugin, providers=...)
        if err: fail the step loudly (027 conventions)

Keep the 030 invariants: refs (not secrets) in `ctx.step_inputs`, run
transcripts, and audit rows — resolve on a copy handed only to the handler.
ACL: resolve with the TOOL's owning plugin as requester (same as dispatch), so
grants keep meaning something in playbooks.

## Workaround until fixed

Fetch through an `agent_step` instead of a `tool_call` step — agent turns go
through the runtime dispatch gate, so vault refs resolve there.
