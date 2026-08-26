# Phase 1 — Python authoring layer: execution summary

**Status: DONE. Shipped as plugin-playbooks 0.8.0 (2026-08-26).**

## What was built

- **`plugin_playbooks/pblang/`** — the new package.
  - `compiler.py` (~700 lines): `compile_playbook(source, name=)` AST-parses
    (never executes) the restricted-Python playbook language into the existing
    `PlaybookDef` IR. Whitelisted combinators only; three expression modes
    (template / eval / literal); prior step ids become `steps.<id>` roots;
    f-strings become Jinja templates; all errors collected at once with line
    numbers and fix hints (`PlaybookCompileError.issues`).
  - `codegen.py` (~300 lines): `generate_code(pb_def)` renders the IR back to
    code, variable names == step ids, defaults omitted, multiline prompts as
    triple-quoted strings.
  - Load-bearing invariant, tested: `compile_playbook(generate_code(ir))`
    equals `ir` under `defs_equal` (json dump, exclude_none, by_alias).
- **`code TEXT` columns** on `playbooks` + `playbook_versions` (NULL = derive
  via codegen on read). On-load `ALTER TABLE` migration + `backfill_code()`
  that stores generated code only when the round-trip verifies.
- **Tool surface** (`agent_tools.py`): `playbook_propose(code=)` (preferred;
  `definition_yaml=` legacy), `playbook_get_definition` returns code by
  default (`format="yaml"` legacy), `playbook_edit` gains `code=` full-source
  and `old=`/`new=` snippet modes (unique-match enforced), `playbook_validate`
  accepts `code=`. Every non-code write path (YAML edit, REST PUT, promote)
  regenerates or NULLs the stored code — stale code can never survive.
- **Authoring skill rewritten** to teach the code language (module constant
  `_AUTHORING_SKILL_BODY`); both embedded examples are compile-verified by
  `test_skill_examples_compile`.

## Verification

- **72/72 tests pass** (`.venv/bin/python -m pytest tests/ -q`): 29 baseline,
  28 pblang compiler/codegen/round-trip, 14 tool-level (propose/edit/
  get_definition/validate/backfill), 1 skill-examples.
- **Real QA Luna (:8766, luna_dev DB)**:
  - Migration ran on load: `code` column added to both tables; all 5 legacy
    playbooks backfilled with verified codegen.
  - Live agent turn: the agent loaded the rewritten skill and authored
    `qa-code-hello` in clean pblang **on its first attempt** (header + llm +
    tool steps, proper `{{ steps.x.y }}` refs), validated clean, dry-ran with
    correct template resolution.
  - Second turn: agent read the source via `playbook_get_definition` (code
    came back) and made a minimal targeted edit (version 2; version 1
    snapshotted **with its code**).
  - Canvas contract intact: stored `definition` JSON unchanged in shape;
    `playbook.patch` ui_event fired on edit.

## Surprises / decisions made during the phase

1. **Unknown-key validation cannot run on compiled dumps.** The pydantic dump
   of a step carries cross-kind defaults (`fan_in`, `concurrency`,
   `max_iterations` on a `tool_call`), which the unknown-key checker falsely
   flags. Resolution: code path skips `check_unknown_keys` (the compiler
   already rejects unknown kwargs with line numbers — strictly better); YAML
   path still checks the raw `safe_load` mapping so typos are caught.
2. **Jinja filters live only inside strings.** `vars.frontier | length` is not
   parseable as a whitelisted Python expression, so conditions with filters
   must be written as `'{{ ... }}'` strings. The skill teaches this explicitly;
   codegen always emits the braced-string form, so round-trips are stable.
3. **Test env**: the plugin `.venv` was missing dev extras (`aiosqlite`,
   `greenlet`) — installed. Invocation that works:
   `.venv/bin/python -m pytest tests/ -q` from the plugin dir.
4. Skill body moved from ~300 concatenated string fragments to one module
   constant — future phases extend it without string-surgery.

## Reassessment of phases 2–7

- **Phase 2 (manifest + staged flow)** — proceed as planned. Two notes:
  the edit ticket's read stage returns *code* (already true — get_definition
  is code-first); extend `_AUTHORING_SKILL_BODY` for the staged-flow recipe
  and add any new code examples to `test_skill_examples_compile`. The
  stage-aware tool descriptions pattern (flows-belong-in-tool-layer) applies
  unchanged.
- **Phase 3 (candidate/promote)** — unchanged. One addition from this phase:
  promote/rollback must carry the `code` column with the definition (the
  promote route already restores `old_ver.code`; keep that invariant when
  `live_version`/`candidate_version` land — "code travels with definition,
  always" is now a stated rule).
- **Phase 4 (specs)** — unchanged. The dry-run `references` namespace proved
  accurate in live QA; the assertion evaluator can rely on it.
- **Phase 5 (probes)** — unchanged.
- **Phase 6 (UI)** — the *Code* tab is cheaper than planned: `GET
  /playbooks/{name}` currently returns `definition` only, so add `code`
  (derive via codegen when NULL — same helper the tools use) to that response;
  the tab is then a read-only render. Replay removal still independent, can be
  pulled forward if a phase runs light.
- **Phase 7 (cleanup)** — smaller than planned: eager migration is already
  done (all rows on luna_dev carry code; backfill is idempotent on every
  load), so phase 7 is just YAML-input removal from the three tools + docs +
  the dojo gates-beat-prose scenario. When `definition_yaml=` is removed, the
  `check_unknown_keys` machinery in agent_tools can go with it (REST paths
  keep their own validation).

No re-ordering needed; dependencies hold.
