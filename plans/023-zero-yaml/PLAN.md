# 023 — Zero YAML

Playbook authoring moved to Python (plans/002); specs and definitions are
stored as JSON. But YAML input paths, a YAML export, a PyYAML dependency, and
stale "whole-YAML" docstrings survive. Roy's direction (2026-09-03): **zero
YAML traces** — the switch to Python is complete only when no YAML can enter
or leave the plugin.

## Inventory (0.39.0)

Working YAML paths:
- `routes.py` — `PlaybookCreate`/`PlaybookUpdate` take `definition_yaml`;
  `parse_yaml` + `_yaml.safe_load` on create/update. No UI caller exists
  (no `ui/` dir); only ad-hoc REST clients.
- `agent_tools.py` `playbook_validate` — accepts `definition_yaml`, parses it.
- Spec tools — single `spec_yaml`, batch `specs` as a YAML document;
  `spec_from_run` *returns* `spec_yaml` (YAML dump).
- Export/read tool — `format='yaml'` dumps the YAML IR, with hint text
  steering the model toward it.

Infrastructure: `definition.py` `parse_yaml`/`to_yaml`; `import yaml` in
`definition.py`, `specs.py`, `agent_tools.py`; PyYAML in pyproject deps.

Text the model reads: module docstring still claims "authoring is whole-YAML
only" (wrong since 0.14.0); tool descriptions mention YAML.

Deliberate keepers: the `definition_yaml=` refusal shims in propose/edit —
they exist to steer stale callers to Python and never parse the value.

## Changes

1. **routes.py** — `definition` (JSON object) replaces `definition_yaml` in
   create/update bodies; validate via `PlaybookDef.model_validate`. (The
   2026-09-03 notify_chat edit already sent JSON through the YAML field —
   same bytes, honest field name.)
2. **playbook_validate** — drop the `definition_yaml` parsing branch; `code=`
   or `name=` only, with a refusal shim for `definition_yaml`.
3. **Spec tools** — `spec` / `specs` take JSON objects (tool args are JSON
   already); `spec_from_run` returns the spec as JSON. Refusal shim for
   `spec_yaml`.
4. **Export** — `format='yaml'` removed; JSON only. Hints rewritten.
5. **definition.py / specs.py** — delete `parse_yaml`, `to_yaml`,
   `parse_spec_yaml`, `parse_spec_batch_yaml`; drop `import yaml`
   everywhere; drop PyYAML from pyproject.
6. **Docstrings/descriptions** — rewrite every YAML mention; the only
   allowed occurrences are the refusal-shim strings.
7. **validation.py** — `check_unknown_keys` still runs on dicts (now from
   JSON); comments updated.

## Tests

- Grep-gate test: no `import yaml`/`parse_yaml`/`definition_yaml`(outside
  shims)/`spec_yaml`(outside shims) in the package.
- REST create/update with JSON definition round-trips; YAML body → 422.
- validate/spec/export tools: JSON forms work; YAML kwargs → steering error,
  never a parse.
- Existing suite stays green (specs still validate from stored JSON).

## Ship

Version 0.40.0 (all three stamps), test locally, push, publish to
marketplaces, upgrade Scanny via marketplace route, verify /api/plugins
and a spec run live. Fleet pin bump rides the next image build.
