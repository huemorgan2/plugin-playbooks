# 022 execution summary — 0.39.0 (truthful evidence, open reads)

Executed 2026-09-03, autonomously, as Phase 1 of the meltdown recovery
(`research/playbooks-meltdown-2026-09-02/RECOVERY.md`).

## What shipped

- **P1 — evidence truthfulness.** `test_run_gate` now returns a 4-tuple:
  the evidence slot only ever holds a PASSED run; a failed latest run rides
  in a dedicated fourth slot. Approval cards, ops-chat announces, the
  `playbook.published` bus event, and the publish tool result all state the
  real status (`passed` / `failed` / `none`) — the word "green" appears only
  for a run that passed. The meltdown's "green evidence" that was actually a
  failed run is structurally impossible now.
- **P2 — approvals fail closed.** Only a truly headless context (ctx=None)
  publishes ungated. A live context with a broken/unwired approval engine, or
  an exception during the approval wait, aborts with "Approval not obtained —
  nothing was published."
- **P3 — spec provenance.** `copy_specs` stamps `carried_from` (the ORIGINAL
  author version, preserved across multi-hop mints via setdefault).
  `spec_list` shows it; deleting a carried spec requires `why=`.
- **P4 — coding-agent-grade reads.** Four new read tools, all
  planning+building, auto-approve: `playbook_versions` (full history
  listing), `playbook_version_read` (code/manifest/specs/runs of ANY
  version), `playbook_version_diff` (unified diff), `playbook_runs`
  (version/status filters, FULL untruncated failure output per failed step).
- **P5 — honest dry runs.** Dry tool_call steps run the same registry
  existence check as live (unknown tool fails the dry run even when
  stubbed); unstubbed stubs self-describe (`_note: "simulated — tool was NOT
  called" / "code was NOT executed"`).
- **P5b — typed inputs, loud conditions.** `_coerce_inputs` casts trigger
  inputs through the declared inputs_schema before the run starts (stored
  inputs == runtime inputs; the int-itemId poison coerces to string).
  `_run_condition` evaluates strict — an un-evaluable `when` fails the step
  loudly instead of silently taking else.
- **P6 — history integrity.** One shared `_dup_keep_key` (content > lineage
  > age) for both `get_version_row` and the duplicate healer; restore paths
  skip empty/NULL manifests instead of nulling the live one; `mint_version`
  flushes and asserts the row was actually written (no more rowless
  versions reported as success).

## Verification

- 350/350 tests pass, including 15 new tests in
  `tests/test_plan022_truthful_evidence.py` covering every P above.
- Five pre-existing tests updated for intended behavior changes (4-tuple
  unpack, self-describing stub shape, carried_from in copied specs, `why=`
  on carried-spec delete, mode/toml manifests for the 4 new tools).

## Deployment

- 0.39.0 committed (7cf055e), pushed to huemorgan2/plugin-playbooks, published
  to marketplaces.com.ai (`official`), upgraded on Scanny via the marketplace
  route — live verification: version 0.39.0 active, needs_restart false,
  29 tools, all four new read tools registered.
- Phase-0 carry-over closed: candidate-intake's stale `candidate_version=44`
  cleared to NULL by direct DB update (guarded: only if still 44) — no REST
  route can clear a candidate, and promote-without-version would have
  regressed live 45→44. API confirms live=45, candidate=null.

## Learnings

- **The shared EXPLANATION fixture contains the word "green"** — asserting
  "green not in card" must target the Evidence line, not the whole
  explanation (the agent's own prose may say anything).
- **SQLAlchemy models can't be `__new__`-ed in tests** (no
  `_sa_instance_state`); use SimpleNamespace for pure-function harnesses
  (`_coerce_inputs`, dry_run only needs `.definition`/`.inputs_schema`).
- **`playbook_versions.manifest` is NOT NULL in fresh schemas** — the NULL
  manifests on Scanny are legacy-DB damage; the falsy guard (`row.manifest`
  truthiness) covers both NULL and empty string, and tests simulate with "".
- **The manifest drift tests are the real gate for new tools**: adding a
  tool means toml `[[tools]]` entry + `requires.tools` count + the
  build/operate mode-set test, or CI fails. Good — three stamps stay honest.
