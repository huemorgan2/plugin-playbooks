# 023 — Zero YAML: execution summary

Shipped as **0.40.0** (2026-09-03). The package contains no YAML: no import,
no parser, no dump, no dependency, no doc text — the only occurrences are the
refusal shims that steer stale `definition_yaml=`/`spec_yaml=` callers to
Python/JSON, and `tests/test_zero_yaml.py` greps the package on every run so
a YAML path can never quietly return.

## What changed

- **REST** — `POST/PUT /playbooks` take a JSON `definition` object
  (validated by `PlaybookDef.model_validate`); `definition_yaml` is gone.
  The UI api client (`ui-src/src/playbooks/api.ts`) was updated and the
  bundle rebuilt (no UI screen actually called create/update — the field was
  dead client code).
- **playbook_validate** — `name=`/`code=` only; `definition_yaml` answers
  with a steering error.
- **Spec tools** — `playbook_spec_add` takes `spec=` (JSON object) or
  `specs=` (JSON object of name → body); `spec_yaml` is a refusal shim.
  `playbook_spec_from_run` returns `"spec"` as JSON.
  `playbook_version_read` returns `"definition"` (JSON) instead of
  `definition_yaml`.
- **Export** — `playbook_get_definition(format='json')` replaces
  `format='yaml'`.
- **Deleted** — `definition.parse_yaml`/`to_yaml`,
  `specs.parse_spec_yaml`/`parse_spec_batch_yaml` (replaced by
  `parse_spec`/`parse_spec_batch` over dicts), every `import yaml`, and the
  PyYAML dependency.
- **Docs** — agent_tools module docstring (still claimed "authoring is
  whole-YAML only", wrong since 0.14.0), tool descriptions, validation.py
  and pblang/compiler.py comments rewritten.

## DB

Checked before coding: `playbooks.definition`, `playbook_versions.definition`
and `playbook_specs.spec` are JSON columns holding JSON — no YAML text is
stored anywhere, so no migration was needed.

## Verification

- 354 tests pass locally, including the new grep-gate + shim-steering tests
  and the ported YAML→JSON fixtures (pblang round-trips, spec suites, REST
  versioned-spec flows).
- Published 0.40.0 to marketplaces.com.ai/official; upgraded Scanny live:
  `installed_version` 0.40.0, `running_source` marketplace, 29 tools, no
  error, `needs_restart` false.
- Fleet pin updated (job-dachk2favr4c73f9chcg): playbooks 0.40.0
  sha256 5e57a0ba… — bakes into the next image build.

## Learnings

- The "no ui/ dir" claim in the plan inventory was wrong — a built React UI
  ships inside `plugin_playbooks/ui/` with source in `ui-src/`. Its api.ts
  still declared `definition_yaml`, unused by any screen. Inventory must
  grep built assets too.
- The owner-edit-via-CDP recipe (PUT with `definition_yaml:
  JSON.stringify(...)`) is obsolete from 0.40.0 — the field is `definition`
  and takes the object directly. Memory updated.
- Tests that author fixtures in YAML (pblang round-trip suites) convert
  cleanly to dict literals — authoring tests in the storage format removes
  the last excuse for a yaml import.
