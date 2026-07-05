# plugin-playbooks

The **Playbooks engine** for [Luna](https://github.com/huemorgan/luna): durable,
multi-step agent workflows. Playbooks are reusable YAML templates of steps —
tool calls, LLM steps, agent decisions, loops, branches, waits — that Luna
authors through conversation and executes on triggers or on demand.

Extracted from Luna core in 009.001 (Luna ≥ 0.29). History up to Luna 0.29.006
lives in the main luna repo.

## What you get

- **10 tools** — `playbook_propose`, `playbook_edit`, `playbook_list`,
  `playbook_get_definition`, `playbook_validate`, `playbook_dry_run`,
  `playbook_run`, `playbook_status`, `playbook_cancel`, `playbook_set_autonomy`.
- A **Playbooks** sidebar section with a full visual canvas (react-flow):
  list → canvas/YAML editor → run replay with per-step state visualization.
  Served as a full-pane iframe from the prebuilt `plugin_playbooks/ui/` dist
  (source in `ui-src/`, Vite + React).
- **Live agent edits** stream into the canvas over Luna's E12
  `ui.plugin.event` bridge (`playbook.open`, `playbook.patch`, `navigate`);
  run/step activity rides the `activity.*` SSE feed.
- **External triggers** — cron, webhooks, connector events — bound through the
  SDK `TriggerSourceRegistry`.
- Chat messages sent from playbook runs are tagged `source=playbook` (E11) for
  generic badging in core.

## Owns its own DB tables (SDK enabler E4)

Five tables (`playbooks`, `playbook_versions`, `playbook_runs`,
`playbook_step_runs`, `playbook_drafts`) are created on enable via
`ctx.engine`, on the plugin's own `MetaData`:

```python
from luna_sdk import declarative_base, JSONB, UUID
Base = declarative_base()   # isolated from core metadata
```

No `import luna.*` — only `luna_sdk` + stdlib + FastAPI/SQLAlchemy/pydantic/
PyYAML/Jinja2. Core migrations and uninstall stay clean.

## Install

Published on the Luna official marketplace
(`https://luna-marketplaces.onrender.com/mp/official`) — install from Luna's
Marketplace section. The artifact is the `plugin_playbooks/` package tree,
zipped deterministically; Luna verifies its sha256 against the marketplace
index before loading.

## Development

```bash
pip install -e ".[dev]"
pytest                      # manifest/contract tests, no Luna runtime needed
```

UI development:

```bash
cd ui-src
npm install
npm run build               # emits into ../plugin_playbooks/ui/
```

The repo tests stub `luna_sdk` (it ships with the Luna runtime, not PyPI) so
the package imports standalone; full behavioral suites run in the luna repo
against the built plugin set.

## License

MIT
