# plugin-playbooks

The **Playbooks engine** for [Luna](https://github.com/huemorgan/luna): durable,
multi-step agent workflows. A playbook is written as **code** (pblang — a
restricted Python subset that is compiled to a step graph, never executed),
carries a plain-text **manifest** stating its intent, and is guarded by
**probes** (are the tools it uses actually installed and answering). Changes
land as **candidates** and only go live through `publish`, which runs
validation → a green test run → probes as gates.

Extracted from Luna core in 009.001 (Luna ≥ 0.29). History up to Luna 0.29.006
lives in the main luna repo.

## Tools

Always visible (run/inspect):

| Tool | What it does |
| --- | --- |
| `playbook_list` | List playbooks with status, versions, trust summary |
| `playbook_run` | Start a live run (background; returns run_id) |
| `playbook_status` | Poll a run / list recent runs |
| `playbook_cancel` | Stop a running playbook |

Skill-gated behind **playbook-authoring** (load the skill; the tools unlock on
the next turn):

| Tool | What it does |
| --- | --- |
| `playbook_propose` | Create a new playbook from code (saved as a candidate) |
| `playbook_edit` | Edit: full `code=` or targeted `old=`/`new=` snippet; read-stage ticket flow enforces manifest+code reading first |
| `playbook_edit_force` | Escape hatch past the drift check; gates still run at publish |
| `playbook_get_definition` | Read code / compiled definition / manifest |
| `playbook_validate` | Static-check code or YAML without saving |
| `playbook_dry_run` | Simulate a run (no tools/LLMs executed; scriptable via `stubs`), returns the trace |
| `playbook_manifest_set` | Write the plain-text intent manifest |
| `playbook_publish` | Candidate → live, through validation/test-run/probes gates |
| `playbook_rollback` | Point live back to an earlier version |
| `playbook_run_candidate` | Live-run the candidate version once |
| `playbook_set_autonomy` | Ask-first / autonomous execution modes |
| `playbook_list_available_triggers` | Cron/webhook/connector trigger catalog |
| `playbook_preflight` | Probe the playbook's tools: installed and answering? |

YAML authoring was removed in 0.14.0 — `playbook_propose` / `playbook_edit`
accept code only (`playbook_validate` still checks YAML as a legacy input).

## Manifest conventions

The manifest is **plain text, not structured** — it states what the playbook
is for, what it must always do, and what it must never do. The edit flow makes
the agent read it before any change (read-stage ticket), and a drift check
blocks edits that contradict it (`playbook_edit_force` is the deliberate
override). Keep it short: a few sentences of intent, then hard constraints,
e.g. "Never send more than one chat message per run." Update it in the same
breath as any change that shifts intent — a stale manifest blocks future
edits for the wrong reason.

## Dry-run stubs

`playbook_dry_run` is a **simulation** — templates render and branching
executes, but tools and LLMs are stubbed. Pass `stubs` (a JSON object) to
script what the stubbed steps return:

```json
{"fetch_weather": {"temp_c": 31, "sky": "clear"}}
```

Notes:

- `stubs` values ARE the result payload (no `result:` wrapper). Step-id keys
  win over tool-name keys.
- After a real run (even a failed one), the recorded step outputs from
  `playbook_status` make good stubs — copy them in keyed by step id to
  reproduce the run's path.
- A dry run is never run evidence: the publish gate wants a green **real**
  test run of the candidate (`playbook_run_candidate`).

## UI

A **Playbooks** sidebar section (full-pane iframe, prebuilt
`plugin_playbooks/ui/`, source in `ui-src/` — Vite + React + react-flow). List
shows per-playbook trust rows (tools · intent) and pending-candidate
chips; the editor has five tabs: Canvas (live/candidate graph, past-run
projection; python playbooks: graph derived from the code, run trace from the
journal), Code (read-only pblang), Manifest, Connections (tool probes),
Runs (stats + per-step execution rows). Live agent edits stream in over
Luna's E12 `ui.plugin.event` bridge; run/step activity rides `activity.*` SSE.

## Owns its own DB tables (SDK enabler E4)

Tables (`playbooks`, `playbook_versions`, `playbook_runs`,
`playbook_step_runs`, `playbook_drafts`, …) are created on enable via
`ctx.engine`, on the plugin's own `MetaData`:

```python
from luna_sdk import declarative_base, JSONB, UUID
Base = declarative_base()   # isolated from core metadata
```

No `import luna.*` — only `luna_sdk` + stdlib + FastAPI/SQLAlchemy/pydantic/
PyYAML/Jinja2. Core migrations and uninstall stay clean.

## Install

Published on the Luna official marketplace — install from Luna's Marketplace
section. The artifact is the `plugin_playbooks/` package tree, zipped
deterministically; Luna verifies its sha256 against the marketplace index
before loading.

## Development

```bash
pip install -e ".[dev]"
pytest                      # engine/tools/probes tests, no Luna runtime needed
```

UI development:

```bash
cd ui-src
npm install
npm test -- --run
npm run build               # emits into ../plugin_playbooks/ui/
```

The repo tests stub `luna_sdk` (it ships with the Luna runtime, not PyPI) so
the package imports standalone; full behavioral suites run in the luna repo
against the built plugin set.

## License

MIT

## Vision

The durable product shape — including the architecture pieces no refactor may remove —
lives in [vision/vision.md](vision/vision.md). Read it before any structural change.
