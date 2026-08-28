# Execution summary — plans/011 integration icons (0.24.0)

Shipped in `feat/011-integration-icons`, merged to main as 0.24.0.

## What shipped

**Backend** — new authed `GET /api/p/plugin-playbooks/reference/icons`
(`plugin_playbooks/routes.py`):
- `tools`: `{tool_name: owning_plugin}` from the runner's ToolRegistry.
- `triggers`: `[{event_pattern, source, app, label, plugin}]`; publisher plugin
  resolved via `TriggerSourceRegistry._by_plugin` ownership (accessed
  defensively — unknown source → `plugin: null`, entry still listed).
- Memoised 300s (`_reset_icon_cache()` on `init_routes`); every registry access
  guarded — failures degrade to empty sections, never a 5xx.
- `init_routes(...)` now takes `trigger_sources=ctx.trigger_sources`.

**UI** — `ui-src/src/playbooks/icons.tsx`:
- `buildIconRef(reference, plugins, connectors)` joins three sources:
  the new reference endpoint, `/api/plugins` (`has_image` filter — plugins
  without a real icon keep the kind glyph, never the generic default png),
  and `/api/p/plugin-connectors/status` (Composio CDN logos by app slug).
- Trigger resolution order: connectors app logo → publisher plugin icon → null.
  Pattern match supports `*` wildcards, dots escaped.
- Module-level cached loader + `useIconRef()` hook; `IntegrationIcon` renders
  `<img>` with onError fallback to the original lucide glyph.

Wired into 6 render sites: StepNode, TriggerNode (cron keeps the Clock),
StepDetailPanel header, explain StepList rows, ToolCallExplain inline pill,
TestsTab ProbeRow. **RunsTab StepExecRow intentionally skipped** —
`StepRunDetail` carries no `tool` field; threading the playbook def down was
out of scope for a calm-surface win (plan allowed the skip).

**Versions** — 0.24.0 in all three stamps: `pyproject.toml`,
`plugin_playbooks/luna-plugin.toml`, `PluginManifest` in `__init__.py`.

## Verification

- pytest: 215 passed (incl. 4 new in `tests/test_icon_reference.py`).
- vitest: 109 passed across 13 files (incl. 10 new in `icons.test.ts`).
- `npm run build` clean; dist committed to `plugin_playbooks/ui/`.
- Live verification on QA Luna after deploy (see plan phase 3).

## Not done (optional phase 4 — separate ships)

- `assets/icon.png` for plugin-stackone / plugin-webhooks.
- Per-provider stackone logos.
- Persist `plugin` in `PlaybookProbeResult`.
