# Make direct recurring playbook triggers runnable while the owner is away

## Failure

The 2026-09-18 dojoP `long.scheduled-fire-proof-12-turns` baseline built and published the correct queue playbook, bound a paused direct playbook trigger with exact inputs, and passed its manual output checks. The independent signed controller fire was admitted, but the resulting playbook run parked on a per-run approval and never produced `output/fired.json` in the 120-second observation window (10/12 checks). The playbook retained its default `agent_must_confirm` autonomy; Luna never called `playbook_set_autonomy` when configuring the owner-authorized unattended schedule. The closeout accurately reported the parked state.

## Change

1. In the always-visible existing-playbook guidance, explain that a direct unattended trigger is not ready while the playbook is `agent_must_confirm`: each fire parks. When the owner explicitly authorizes unattended execution, set `agent_may_trigger` through the normal gated tool, then verify the stored mode and a real fire. Preserve `manual_only` and per-run confirmation when the owner requests them.
2. Keep the run gate itself unchanged. A schedule request does not silently bypass approval; the autonomy change still goes through its existing owner card. Do not report readiness from trigger creation alone.
3. Retest with the full default-plus-DB-and-Playbooks setup and signed controller fire; score the persisted output and run receipt, not a successful fire admission. Run the plugin regression suite and bump the version if the candidate has already been published.
