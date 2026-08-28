# 012 / phase 3 — payload diet

Status: DRAFT (to be finalized after phase 2's execution summary; update
this file with phase-2 learnings before starting).

## Scope (from PLAN.md, pre-phase-2 draft)

1. Read stage: replace the full `LANGUAGE_CHEATSHEET` (~8KB) block with a
   mini-reference (~15 lines: step kinds, ref shapes, the three rules
   agents actually forget) + "full reference: playbook_language_reference".
   The full sheet stays one call away; the recall POINTER survives.
2. `_AUTHORING_SKILL_BODY` audit: ~24KB → ≤12KB. Reference-grade detail
   (full YAML key tables, long examples) moves into
   `playbook_language_reference` output; the skill keeps rules + workflow.

## Verification

- Byte-size assertions in tests: read stage ≤6KB before the code block;
  skill body ≤12KB.
- Full suite green; contract tests from phase 2 still pass (frames
  unchanged, only the language-reference block replaced).
- Real-Luna probe: skill loads, edit flow works, mini-reference visible
  in read stage.

## Ship

Minor bump, push, publish, tenant upgrade + verify.
