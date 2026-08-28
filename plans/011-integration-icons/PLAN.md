# 011 — Integration icons in playbooks UI

## Goal

Tool steps and triggers in the playbooks UI show the icon of the integration they belong to, instead of the generic wrench/zap:

- A `monday.get_board` step shows the monday plugin icon.
- A Composio trigger (e.g. `connector.gmail.new_email`) shows the actual integration logo (Gmail), sourced from Composio metadata.
- Anything unresolvable keeps the current kind icon (wrench for tool_call, zap for trigger). Never a broken image.

Scope: plugin-playbooks only. No luna core changes (no `ToolDef`/`TriggerInfo` schema edits).

## Facts (from research, 2026-08-28)

- Wrench is hardcoded per step kind in two icon maps: `ui-src/src/playbooks/nodes/StepNode.tsx:33-46` and `ui-src/src/playbooks/explain/primitives.tsx:18-30`. Trigger node uses only Clock/Zap (`nodes/TriggerNode.tsx:8,62-63`).
- The UI has no tool metadata — a tool is a bare string (`types.ts:33`). No tool-catalog endpoint exists.
- The server already knows the owning plugin per tool: `runner._tools` is the core `ToolRegistry` (`runner.py:142-148`); `RegisteredTool.plugin` (luna `tool_registry.py:24-56`). `probes.py:74-119` even computes `plugin` per probe and drops it.
- Core serves a **public, auth-free** plugin icon: `GET /api/plugins/{name}/icon` (luna `plugin_api/app.py:1795-1834`), always 200, falls back to a default image. Core UI already hotlinks it (`SettingsPanel.tsx:1258-1275`).
- Composio logos: plugin-connectors persists `logo` (Composio CDN URL) per toolkit slug in its app state (`plugin_connectors/__init__.py:387-410`), exposes it via `GET /api/p/plugin-connectors/catalog` and `/status`. Its `TriggerInfo.app` is the real toolkit slug (`plugin_connectors/triggers.py:65-70`) — the join key.
- Other trigger sources: monday `app="monday"`, webhooks `app="webhooks"`, stackone `app="stackone"` (provider only inside `event_pattern`, e.g. `stackone.bamboohr.employee_created`).
- `TriggerSourceRegistry.all_triggers()` is already consumed by playbooks (`trigger_bindings.py:40-57`): `TriggerInfo{slug, source, app, label, event_pattern, ...}` — no logo field.
- UI build: `ui-src` vite → committed `plugin_playbooks/ui/` dist; cache-busted by toml version (`routes.py:83-106`).
- Version 0.16.0 in three stamps: `__init__.py` PluginManifest (authoritative), `luna-plugin.toml`, `pyproject.toml`.

## Design

One new read-only endpoint in plugin-playbooks aggregates everything the UI needs; the UI resolves an icon URL per tool/trigger with a graceful fallback chain.

### Phase 1 — backend: icon reference endpoint

`GET /api/p/plugin-playbooks/reference/icons` →

```json
{
  "tools": { "<tool_name>": { "plugin": "plugin-monday", "icon_url": "/api/plugins/plugin-monday/icon" } },
  "triggers": [ { "event_pattern": "connector.gmail.*", "source": "connectors", "app": "gmail",
                  "label": "Gmail", "icon_url": "https://<composio-cdn>/gmail.png" } ]
}
```

- Tools: iterate `runner._tools` registry → `{tool: plugin}`; `icon_url` = core public icon route.
- Triggers: iterate `TriggerSourceRegistry.all_triggers()`.
  - `source == "connectors"`: join `info.app` → logo from plugin-connectors app state. Access via a guarded plugin-instance lookup (NOT a direct import — managed plugins load as `luna_plugin_<name>`, see memory `cross-plugin-imports-loader-module-names`); on any failure, `icon_url = null`.
  - Other sources: map source → owning plugin (small explicit map: `monday → plugin-monday`, `webhooks → plugin-webhooks`, `stackone → plugin-stackone`, else null) → core icon route.
- Cache the assembled payload in-process ~5 min (connectors state read is cheap but avoid per-render work).
- Auth: same cookie auth as the other playbooks routes.
- Tests: endpoint shape; connectors-absent degradation (icon_url null, no exception); unknown source → null.

### Phase 2 — UI: icon resolution + rendering

- `api.ts`: `getIconReference()` + a module-level cached fetch (once per session, non-blocking — UI renders with kind icons, upgrades when the reference arrives).
- New `IntegrationIcon` component: `<img src={icon_url}>` (rounded, sized to slot), `onerror`/missing → the existing kind icon (Wrench/Zap/Clock). Cache-bust core-icon URLs with `?v=` only if needed (core route already sets `max-age=300`).
- Wire into every per-tool/per-trigger slot found in research:
  1. Canvas step node icon square — `StepNode.tsx:95-108` (tool_call only; other kinds unchanged).
  2. Explain panel header — `PlaybookEditor.tsx:1087-1118`.
  3. Nested step rows — `explain/primitives.tsx:235-243`.
  4. Tool name pill in tool_call explainer — `explain/registry.tsx:18-33`.
  5. Trigger canvas node — `TriggerNode.tsx:62-63`: match the playbook's trigger `event` against `triggers[].event_pattern` (translate `*` wildcards, same semantics as `trigger_bindings`); cron triggers keep Clock.
  6. Run step rows — `RunsTab.tsx:260-300` (currently no icon; add one only if it stays calm — single 14px glyph before the step id).
  7. Probe/connection rows — `TestsTab.tsx:223-247`.
- UX (per `vision/ux_guidelines.md`): icons are quiet identity marks, not decoration — 14–16px, rounded 4px, no new text, no tooltips for basic meaning; keep tinted-square treatment on canvas nodes (logo replaces the glyph inside the same square).

### Phase 3 — build, verify, ship

- `cd ui-src && npm run build` (commits dist).
- Bump 0.16.0 → 0.17.0 in all three stamps.
- Full test suite.
- Verify on the real QA Luna (memory `verify-plugins-on-real-luna`): playbook with a monday tool step shows the monday icon; a connectors trigger shows the Composio logo; a plugin without an icon falls back cleanly; connectors uninstalled → wrench/zap, no console errors.
- Push, then publish to marketplaces.com.ai (memory `always-ship-after-push`).

### Phase 4 (optional, separate ships)

- plugin-stackone and plugin-webhooks declare `image="assets/icon.png"` but ship no `assets/` dir → their tools will show the core default image. Add real `assets/icon.png` to each (own version bump + publish).
- Stackone per-provider trigger logos: parse provider from `event_pattern` (`stackone.<provider>.<event>`) and join to StackOne catalog `logo_url` (`plugin-stackone/routes.py:145`) — only if Roy wants provider-level (BambooHR) rather than plugin-level (StackOne) icons.
- Persist `plugin` in `PlaybookProbeResult` (`models.py:209-230`) so probe rows don't depend on the live registry.

## Risks

- Composio CDN hotlinking: already the accepted pattern in the connectors settings UI; `onerror` fallback covers outages.
- Cross-plugin state access to connectors: must degrade to null on any error — playbooks must render identically with connectors absent.
- Icon endpoint is public but the reference endpoint is authed; no new data exposure (tool→plugin mapping is not sensitive).
