# 001 — run stats in the playbook list

**Status: SHIPPED 2026-07-23 in 0.4.0.**

Owner ask: in the playbook list, show **when each playbook last ran** and
**how often it runs per day** — and make sure loading the Playbooks surface
does not hammer the database.

---

## Today

`GET /playbooks` returns one row per playbook straight from the `playbooks`
table. The list card shows name, description, version, autonomy. Nothing says
whether a playbook is alive: a thing that last ran in April looks identical to
one that runs six times a day.

Run history lives in `playbook_runs` (`playbook_id`, `started_at`, …), read
today only by the per-playbook runs drawer.

## What ships

Two facts per row, in the metadata line that already carries `v3 · agent must
confirm`:

- `ran 2h ago` — the most recent run, ever. `never run` when there is none.
- `3.4/day` — runs per day over the last 30 days.

Rate wording is a number, not a sentence (ux_guidelines §5). The unit is
explained once by an `(i)` on the column header area — never hover-only
meaning (§6); the number reads fine without it.

## The database rule

The surface loads on every visit to Playbooks and again after every
create/archive/enable. So the cost must be **flat in the number of runs and
flat in the number of playbooks** — no per-row query, no full-table scan.

1. **One grouped query, windowed.** A single
   `SELECT playbook_id, COUNT(*), MAX(started_at) FROM playbook_runs WHERE
   started_at >= now() - 30d GROUP BY playbook_id` covers both numbers for
   every playbook that ran recently. One round trip regardless of list size.
2. **A second, bounded query only when needed.** Playbooks absent from (1)
   have not run in 30 days; their `last_run_at` comes from one more grouped
   query restricted to exactly those ids (`WHERE playbook_id IN (…)`). It is
   skipped entirely when every playbook ran recently, and its group count is
   bounded by the number of idle playbooks — never by run volume.
3. **An index that makes both index-range scans**:
   `ix_playbook_runs_playbook_started (playbook_id, started_at)`. Existing
   installs get it too — `table.create(checkfirst=True)` does not add indexes
   to a table that already exists, so `on_load` creates each index explicitly
   with `checkfirst=True`.
4. **A short TTL cache (20s)** in front of the aggregate. The pane refreshes on
   its own events; repeat loads inside the window reuse the computed stats.
   Stats are a trailing indicator — 20s of staleness is invisible, and it caps
   the query rate at 3/min per process no matter how often the pane mounts.

Non-goals: no new columns, no ALTER on a live tenant's tables, no write-path
change in the runner. This is a read-path feature only.

## Rate definition

`runs_per_day = runs_in_window / days_observed`, where `days_observed` is the
days the playbook has actually existed inside the window:
`clamp(days since max(created_at, window_start), 1, 30)`.

A playbook created yesterday that ran 4 times reads `4.0/day`, not `0.1/day`.
Rounded to one decimal; below `0.05` it reads `<0.1/day` rather than `0.0/day`,
which would look like "never" next to a real last-run time.

## Steps

1. `models.py` — declare the composite index on `PlaybookRun`.
2. `__init__.py::on_load` — create indexes explicitly after table creation,
   guarded so an unindexable/legacy DB cannot block plugin load.
3. `routes.py` — `_run_stats(session, playbook_ids)` returning
   `{id: {last_run_at, runs_per_day, runs_window}}`, plus the TTL cache;
   `list_playbooks` merges it in.
4. `types.ts` / `PlaybooksSection.tsx` — two more items in the metadata line.
5. Tests — sqlite-backed: the aggregate is one query for many playbooks,
   windowing is respected, the rate maths, idle-playbook fallback, cache TTL.
6. Version bump (3 stamps), push, publish to marketplaces.com.ai.

---

## What shipped (0.4.0)

- `models.py` — `ix_playbook_runs_playbook_started (playbook_id, started_at)`.
- `__init__.py::on_load` — indexes created one by one with `checkfirst=True`,
  so installs that predate the index get it; failures log and never block load.
- `routes.py` — `_run_stats()`: one grouped windowed query, a second grouped
  query bounded to the idle ids only, a 20s TTL cache, and a `_reset_stats_cache()`
  fired when the owner starts a run. `list_playbooks` merges `last_run_at`,
  `runs_per_day`, `runs_window` and falls back to zeroes if the aggregate fails.
- `runStats.ts` / `PlaybooksSection.tsx` — `ran 2h ago` · `3.4/day` in the
  metadata line; `never run` when there is none, `<0.1/day` for a rare runner.
- Tests: 10 sqlite-backed (query counts asserted at 1 and 2 for 50 playbooks,
  windowing, rate maths, cache TTL) + 8 UI label tests.
