# plans/027 — gated registration fails loud, never silently ungates

Source: luna plan 102 phase 9.1 (round-2 context-capture findings).

## Problem

`_register_tool` had two silent fallbacks: a ctx without `skill_registry`,
or a tool registry whose `register()` lacks the `skill_gated` kwarg
(TypeError), both dropped to plain ungated registration. On such a core all
18 authoring/delegation tools appeared on every turn — the skill gate
defeated with no operator-visible signal.

## Fix

Both paths now raise `RuntimeError` naming the tool and the remedy
(upgrade the core / remove the plugin). Non-gated tools are unaffected.
This is a deliberate compat break: a core too old to gate gets a loud load
failure instead of a quietly wider tool surface.

## Tests

`tests/test_plan027_gating_fail_loud.py` (TDD — 2 red on the old code):
- skill_registry=None + authoring tool → RuntimeError, nothing registered
- TypeError from register(skill_gated=True) → RuntimeError, no fallback
- modern core → registered with skill_gated=True
- non-gated tool → plain registration still fine with skill_registry=None
