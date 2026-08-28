# 012 / phase 3 — payload diet

Status: READY (updated 2026-08-28 from phase-2 learnings; baseline
0.20.0 cd7a09f, 189 tests green).

## Learnings carried in from phase 2

- Keep the `--- language reference ---` frame NAME as-is; only its
  CONTENT shrinks to the mini-reference. `tests/readstage.py` and any
  agent habit formed on the frames survive untouched.
- Re-sync before starting: a concurrent session ships versions in this
  repo continuously — `git pull` and re-read the three version stamps
  (in-code manifest, luna-plugin.toml, pyproject.toml) right before
  bumping.
- expire_on_commit: capture ORM attribute values before `commit()` in
  any handler refactor.

## Scope

1. Read stage: the `--- language reference ---` frame carries a
   mini-reference (~15 lines: step kinds, ref shapes, the three rules
   agents actually forget) + "full reference: playbook_language_reference".
   The full `LANGUAGE_CHEATSHEET` stays one tool call away — the
   plans/003 recall POINTER survives; the ~8KB body stops riding on
   every edit. Other cheatsheet attach points (failed validate,
   playbook_language_reference itself) keep the full sheet.
2. `_AUTHORING_SKILL_BODY` audit: ~24KB → ≤12KB. Reference-grade detail
   (full YAML key tables, long examples) moves into
   `playbook_language_reference` output; the skill keeps rules +
   workflow + steering.

## Verification

- Byte-size assertions in tests: read-stage output minus the code and
  manifest sections ≤6KB (header + frames + mini-reference — code and
  manifest sizes belong to the playbook, not the tool); skill body
  ≤12KB.
- Full suite green; phase-2 contract tests untouched and passing.
- Ship-then-verify on the live tenant (hot-load: version, active, tool
  count) — QA-Luna turn probes remain unavailable (other session owns
  :8766).

## Ship

Minor bump, push, publish, tenant upgrade + verify.
