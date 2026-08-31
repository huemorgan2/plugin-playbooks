# 017 — archived playbooks stop squatting their names

From luna `plans/092-prompt-evolution/fix-plan.md` #3. Evidence: FINDINGS §10 —
DELETE archives (`status="archived"`, routes.py:851-862) but
`playbook_propose`'s exists-check (agent_tools.py:209-212) has no status
filter, so an archived name can never be re-created; no route hard-deletes.

## Changes (0.28.1 → 0.29.0)
1. **Propose over an archived name replaces it.** `playbook_propose`: a
   name collision with an ARCHIVED playbook unarchives-and-replaces (the new
   proposal takes the row: status→draft-per-normal-flow, definition replaced,
   run history kept). Collision with a LIVE playbook still errors.
2. **Purge route.** `DELETE /playbooks/{name}/purge` (owner REST only, no agent
   tool): hard-deletes an ARCHIVED playbook + its versions; 409 if not
   archived (archive first — two explicit steps to destroy history).
3. Archive-list UX already exists; no pane work.

## Tests
- propose over archived name succeeds and replaces; over live name still 409.
- purge deletes archived row + versions; purge of live/missing → 409/404.
- existing suite stays green.

## Ship
pytest → bump luna-plugin.toml + PluginManifest to 0.29.0 → commit, tag
v0.29.0, push → package_plugin.py → publish to `official` → verify index.json.
