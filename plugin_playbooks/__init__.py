"""plugin_playbooks — the Playbooks engine for Luna.

Provides durable, multi-step agent workflows. Playbooks are reusable
templates of steps that Luna builds through conversation and executes
on triggers or on demand.
"""

import logging
from datetime import datetime, timezone

from luna_sdk import LunaPlugin, PluginContext, PluginManifest, SidebarSection, SkillDef

logger = logging.getLogger(__name__)


# Columns added after a table first shipped. `table.create(checkfirst=True)`
# skips existing tables entirely, so these need explicit ALTERs. Additive,
# SQLite/PG-safe DDL only.
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    # (table, column, DDL type suffix)
    ("playbooks", "code", "TEXT"),                        # 0.8.0
    ("playbook_versions", "code", "TEXT"),                # 0.8.0
    ("playbooks", "manifest", "TEXT NOT NULL DEFAULT ''"),          # 0.9.0
    ("playbook_versions", "manifest", "TEXT NOT NULL DEFAULT ''"),  # 0.9.0
    ("playbooks", "live_version", "INTEGER NOT NULL DEFAULT 0"),    # 0.10.0
    ("playbooks", "candidate_version", "INTEGER"),                  # 0.10.0
    ("playbooks", "failures_acked_version", "INTEGER"),             # 0.21.0
    # 0.26.0 (plans/015, 089): build/operate split
    ("playbooks", "publish_autonomy", "VARCHAR(16) NOT NULL DEFAULT 'ask'"),
    ("playbook_runs", "report_to", "UUID"),
    ("playbook_runs", "is_test", "BOOLEAN NOT NULL DEFAULT FALSE"),
    # 0.28.0 (plans/016 phase 6): switchable publish gates
    ("playbooks", "publish_require_specs", "BOOLEAN NOT NULL DEFAULT TRUE"),
    ("playbooks", "publish_require_run", "BOOLEAN NOT NULL DEFAULT TRUE"),
    # 0.28.0 (plans/016 phase 5): specs belong to a version
    ("playbook_specs", "playbook_version", "INTEGER NOT NULL DEFAULT 0"),
]

# Indexes whose definition changed — dropped on load so the model's current
# index (a different name) can be created next to them without conflicts.
_LEGACY_INDEXES: list[tuple[str, str]] = [
    # (table, index name)
    ("playbook_specs", "ix_playbook_specs_playbook_name"),   # 0.28.0: now (pb, version, name)
]


async def _ensure_columns(engine) -> None:
    """Add late-added columns to installs that predate them."""
    from sqlalchemy import inspect, text

    def _missing(sync_conn):
        insp = inspect(sync_conn)
        cols = {
            t: {c["name"] for c in insp.get_columns(t)}
            for t in {t for t, _, _ in _COLUMN_MIGRATIONS}
        }
        return [
            (t, col, ddl) for t, col, ddl in _COLUMN_MIGRATIONS
            if col not in cols[t]
        ]

    async with engine.begin() as conn:
        for table, col, ddl in await conn.run_sync(_missing):
            await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
            logger.info("playbooks: added %s column to %s", col, table)


async def _drop_legacy_indexes(engine) -> None:
    """Drop indexes the model no longer declares (see _LEGACY_INDEXES)."""
    from sqlalchemy import inspect, text

    def _present(sync_conn):
        insp = inspect(sync_conn)
        return [
            (t, name) for t, name in _LEGACY_INDEXES
            if any(ix["name"] == name for ix in insp.get_indexes(t))
        ]

    async with engine.begin() as conn:
        for table, name in await conn.run_sync(_present):
            await conn.execute(text(f"DROP INDEX {name}"))
            logger.info("playbooks: dropped legacy index %s on %s", name, table)


async def backfill_spec_versions(session_factory) -> int:
    """0.28.0 (plans/016 phase 5): pin pre-versioned specs (playbook_version
    0) to the playbook's live version, and give the candidate — if one
    exists — its own copy. Idempotent. Returns the number of rows touched."""
    from sqlalchemy import select

    from .models import Playbook, PlaybookSpec
    from .versioning import copy_specs, live_version_of

    touched = 0
    async with session_factory() as session:
        rows = (await session.execute(
            select(PlaybookSpec).where(PlaybookSpec.playbook_version == 0)
        )).scalars().all()
        if not rows:
            return 0
        pids = {r.playbook_id for r in rows}
        playbooks = {
            p.id: p for p in (await session.execute(
                select(Playbook).where(Playbook.id.in_(pids))
            )).scalars().all()
        }
        for r in rows:
            p = playbooks.get(r.playbook_id)
            if p is None:
                continue
            r.playbook_version = live_version_of(p)
            touched += 1
        await session.flush()
        for p in playbooks.values():
            if p.candidate_version:
                touched += await copy_specs(
                    session, p.id, live_version_of(p), p.candidate_version,
                )
        await session.commit()
    if touched:
        logger.info("playbooks: versioned %d spec row(s)", touched)
    return touched


async def backfill_code(session_factory) -> int:
    """Store pblang code for every playbook that has none.

    Each backfill is verified — the generated code must compile back to the
    exact same definition, else the row is skipped (code stays NULL and is
    derived on read). Returns the number of rows backfilled.
    """
    from sqlalchemy import select

    from .definition import PlaybookDef
    from .models import Playbook
    from .pblang import compile_playbook, defs_equal, generate_code

    filled = 0
    async with session_factory() as session:
        rows = (await session.execute(
            select(Playbook).where(Playbook.code.is_(None))
        )).scalars().all()
        for pb in rows:
            try:
                d = PlaybookDef.model_validate(pb.definition)
                code = generate_code(d)
                if defs_equal(d, compile_playbook(code)):
                    pb.code = code
                    filled += 1
                else:
                    logger.warning(
                        "playbooks: codegen round-trip drift for '%s' — "
                        "leaving code NULL", pb.name,
                    )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "playbooks: could not backfill code for '%s': %s",
                    pb.name, e,
                )
        if filled:
            await session.commit()
    if filled:
        logger.info("playbooks: backfilled pblang code for %d playbook(s)", filled)
    return filled


async def backfill_live_version(session_factory) -> int:
    """0.10.0 (plans/002 phase 3): pin `live_version` on pre-0.10 rows.

    0 means "same as version"; make that explicit so every reader can trust
    `live_version` directly. Idempotent. Returns the number of rows updated.
    """
    from sqlalchemy import update

    from .models import Playbook

    async with session_factory() as session:
        result = await session.execute(
            update(Playbook)
            .where(Playbook.live_version == 0)
            .values(live_version=Playbook.version)
        )
        await session.commit()
    n = result.rowcount or 0
    if n:
        logger.info("playbooks: backfilled live_version for %d playbook(s)", n)
    return n


# 0.8.0 (plans/002 phase 1): the authoring skill teaches playbook CODE —
# the restricted-Python language compiled by pblang. Both examples are
# compile-verified in tests/test_pblang.py (test_skill_examples_compile).
_AUTHORING_SKILL_BODY = '''\
## Playbook Authoring

A playbook is written in playbook CODE — a restricted Python dialect. You
WRITE code; Luna PARSES and COMPILES it into a step graph. It is NEVER
executed as Python: no imports, no class, no Python for/while/if, no
comprehensions; the only callables are the combinators, range(), and your
top-level `def` functions (compile-time macros). Each
statement is ONE step: `<id> = combinator(...)` — the variable name is the
step id. EXACT SYNTAX (signatures, loop kwargs, state ops, filters) is one
call away: `playbook_language_reference` — never guess.

### THE LOOP — build a playbook like you write code
Authoring a playbook IS coding. Never run blind:
0. OUTLINE FIRST — write the decomposition as one line per step:
`id -> kind -> the SINGLE operation`. Self-check: (a) any line with a
quantifier (each/all/every) MUST be a `loop`; (b) no single step may carry
the whole task; (c) each `agent()`/`llm()` is ONE judgment on ONE thing.
Only then write code.
1. WRITE: `playbook_propose(name, code=...)` to create; edit via the
two-step ticket flow — see MANIFEST + THE EDIT FLOW below.
2. COMPILE: `playbook_validate(code=... | name=...)` — ALL errors at once
(line numbers, undefined references, unknown tools, bad loops, cycles).
3. TEST: `playbook_dry_run(name, inputs)` — simulates the run with tool/LLM
steps STUBBED: proves loops iterate, branches branch, templates resolve — no
side effects. Tests the CANDIDATE by default. Outputs are SIMULATED: NEVER
report a dry-run value as a real result. Copy your `steps.<id>.<field>`
paths from its `references` (the exact template namespace). The `trace`
per-step `output` key is JUST a trace label — `steps.<id>.output.<field>`
does not exist.
4. RUN: `playbook_run(name, inputs)` for real. Runs execute in the
background: on status 'running', poll `playbook_status(run_id)` until
'done'/'failed'. Never re-run a 'running' playbook, never invent results.
5. INSPECT: `playbook_status(run_id)` — each step's resolved inputs +
outputs (your stack trace). Fix and repeat.

### THE POINT: turn a prompt into a process
A playbook's value is DECOMPOSITION — small, visible, reusable steps with
structured data flowing between them. The whole task in ONE agent() prompt
is a prompt wearing a playbook costume. Don't.

### AGENT DECIDES, THE WORKFLOW WORKS
An `agent()` step is a DECISION node — a judgment needing tools or memory
mid-reasoning. It is NOT where you do the work: fetching, searching,
looping, transforming, storing go in `tool()`, `loop()`, `code()`,
`state()` steps. A paragraph telling an agent to go DO a multi-step job IS
your step list. The validator enforces this:
- `monolithic-playbook` (ERROR — blocks): one delegated step hides the whole
process. Decompose it.
- `compound-leaf` (warning): one leaf's prompt hides a loop or several
operations. Split it.
- `agent-does-work` (warning): mechanical work with no judgment — use
`tool()`. Treat warnings as redesign signals, not noise.

Rules of thumb:
- A step prompt containing 'and then', a numbered list, or 'each' is
probably SEVERAL steps.
- One LLM step = ONE judgment on ONE thing. Iterate with `loop()`, branch
with `if_()`, do deterministic work with `tool()` or `code()`.
- DEFAULT to `llm()` for pure transforms — one raw model call, no tools,
cheaper, faster. Use `agent()` ONLY when the step must call tools or memory.
- Prefer `output=` on llm/agent steps — structured data the next step can
filter/branch on, not prose to re-parse. Gather loop results with `collect=`.

### Worked example — 'scan my emails for subscriptions':
WRONG: one agent() whose prompt loops, classifies, formats, totals.
RIGHT:
```python
playbook(name='subscription-scan', description='Scan emails for paid subscriptions',
    when_to_use='When the owner asks what subscriptions they pay for')

fetch = tool('gmail__gmail__fetch_emails', query='after:2024/12/15 (receipt OR invoice)')
scan = loop(
    over='{{ steps.fetch.result.messages }}', item_name='email', concurrency=4,
    body=[
        (classify := llm('Is THIS ONE email a paid subscription? {{ email }}',
            output={'is_subscription': 'bool', 'service': 'str', 'amount': 'number'})),
    ],
    collect='{{ steps.classify }}',
)
report = llm(
    """Markdown report of: {{ steps.scan.collected | selectattr('is_subscription') | list }}""",
    output={'report': 'str'},
)
notify = tool('send_chat_message', message='{{ steps.report.report }}')
```
Each step is inspectable, `classify` is reusable, the data between steps is
typed. THAT is a playbook.

### SYNTAX ESSENTIALS (full rules: playbook_language_reference)
- FIRST statement: the `playbook(...)` header — `name=` (required),
`description=`, `when_to_use=`, `inputs=` (JSON-schema dict),
`triggers=[trigger(event=..., filter={...}, map={...})]`.
- Step kinds: `tool` `llm` `agent` `code` `if_` `loop` `parallel` `approve`
`wait_event` `subtask` `state` `halt`, value assignment (`x = <expression>`
— computes ONCE into a run-scoped var), top-level `def` (reusable
sequence). In nested lists bind with `(x := llm(...))`.
- `code("""<body>""", inputs={...})` runs jailed Python (no network) for
anything DETERMINISTIC an llm() would only approximate — parsing, math,
dedup, dates. `return` a JSON value; read `steps.<id>.result`. Requires
plugin-inline-code-run on this agent.
- EXPRESSIONS: plain strings pass through verbatim — Jinja `{{ ... }}` goes
inside them; bare Python over `inputs`/`vars`/`steps`/`event` and f-strings
work. Jinja FILTERS only exist inside strings:
`'{{ vars.frontier | length > 0 }}'`. Dot access ALWAYS reads the dict key —
never a Python method.

### CONTEXT ECONOMY — iterate, never dump
The #1 way a playbook fails is dumping a big collection into ONE model call.
To process N items: LOOP, judge ONE per iteration with `llm()`, emit a SMALL
structured result, `collect=` it, operate on the reduced set. NEVER
interpolate a whole collection into one prompt. The validator warns; treat
it as a redesign signal.

### REFERENCE SHAPES — or the run fails LOUD
- `tool()` output is wrapped: `steps.<id>.result.<field>`.
- Schemaless `llm()`/`agent()` returns `{_raw: <text>}` — read
`steps.<id>._raw`. There is NO `.output`. Declare `output=` for typed fields
(`steps.<id>.<field>`). This is the #1 cause of a loop that collected nulls.
- `loop()`: `steps.<loop_id>.collected`. `code()`: `steps.<id>.result`.
Value assignment / state(): `vars.<name>`.
On a wrong-path error, copy the right one from `dry_run`'s `references`.

### RUN-SCOPED STATE + NEVER HARDCODE A DISCOVERABLE LIST (hard rule)
A `state()` step mutates run-scoped vars that PERSIST across loop iterations
(read as `vars.<name>`) — stacks, queues, sets, counters (exact ops:
playbook_language_reference). 'Scan/crawl/traverse a site / tree / paginated
results / graph' MUST discover items at RUN TIME with a frontier — never N
sibling tool() calls to URLs/items you guessed:
```python
playbook(name='site-crawl', description='BFS crawl',
    inputs={'type': 'object', 'properties': {'start_url': {'type': 'string'}}})

seed = state(set_('frontier', '[ inputs.start_url ]'), set_('visited', '[]'))
crawl = loop(
    while_='{{ vars.frontier | length > 0 }}',   # frontier grows + shrinks
    max_iterations=200,
    body=[
        state(
            pop_front('frontier', into='cur'),   # FIFO = BFS
            add_unique('visited', '{{ vars.cur }}'),
            id='take',
        ),
        tool('web_fetch', url='{{ vars.cur }}', id='fetch'),
        (links := llm('List internal link URLs: {{ steps.fetch.result }}',
                      output={'links': 'array'})),
        loop(over='{{ steps.links.links }}', item_name='link', id='enqueue',
            body=[
                if_('{{ link not in vars.visited and link not in vars.frontier }}',
                    then=[state(push_back('frontier', '{{ link }}'), id='push')],
                    id='gate'),
            ]),
    ],
)
```
Swap pop_front→pop_back for DFS. `visited` makes it cycle-safe;
`max_iterations` bounds it (ALWAYS set it on a while_ loop, and mutate a
`vars.*` each iteration or it runs to the cap). PREFER `concurrency=4` on a
side-effect-free `over=` loop body — but never mutate shared state in a
concurrent loop. THAT is a crawl.

### MANIFEST + THE EDIT FLOW (read → ticket → write)
Every playbook can carry a MANIFEST: the owner's intent in plain markdown —
Purpose, Side effects, Never (invariants), Acceptance. Editing is TWO steps:
1. READ: `playbook_edit(name)` alone → a JSON header (versions, ticket)
plus the manifest and current code as plain-text frames. Your edit must stay
within the manifest; copy `old=` snippets verbatim from the code frame.
2. WRITE: `playbook_edit(name, ticket=..., ...)` with exactly one of `code=`
or `old=`/`new=` (the `old` snippet must match exactly one place). The
ticket is single-use and expires; no valid ticket, no save.
On a manifest conflict: fix the code and retry with the SAME ticket (a
refusal does not burn it); or `playbook_manifest_set` (asks the owner); or
`playbook_edit_force` when the owner explicitly wants the change (also
asks). NEVER work around a refusal any other way. Pass `manifest=` to
`playbook_propose` on create; if a playbook has none, propose one.

### CANDIDATE → PUBLISH (a save never changes the running playbook)
Saving an edit creates a CANDIDATE — the LIVE playbook keeps running
unchanged until you publish. Loop: edit → `candidate_saved` (one candidate
max; history keeps every version) → `playbook_dry_run` (candidate by
default) → REAL supervised proof `playbook_run_candidate` (asks the owner)
→ `playbook_publish(name)` — gates: static validation, SPECS, a green test
run since the last edit; a refusal names the failing gate — fix the
candidate, never bypass. `playbook_rollback(name)` restores the previous
live version. NEVER report an edit as done after `candidate_saved` — the
old version runs until publish succeeds.

### SPECS (playbook tests)
A spec is a stored test: fixture `inputs`, scripted `stubs` (step-id or
tool-name → pretended output), and `expect` assertions over the dry-run
trace. Specs run automatically on every candidate save and are a PUBLISH
GATE — a failing spec blocks `playbook_publish` until the code is fixed or
the spec updated.
- Write stubs from recorded reality, not memory: after ANY real run — even
a FAILED one — start from `playbook_spec_from_run(name)`; trim, then save.
- BATCH: ALL the specs you intend to add go in ONE
`playbook_spec_add(name, specs=...)` call (YAML mapping of name → body).
One call per spec wastes the owner's time.
- `playbook_spec_run` runs all specs; `playbook_spec_list` shows last
results. No specs = no safety net — after meaningful changes, propose
pinning one from a good run.
- Keep specs SMALL — assert the few things that matter. Over-tight specs
fail on harmless changes.

### PREFLIGHT (are the tools alive?)
Specs stub the outside world; `playbook_preflight(name)` probes every tool
the playbook touches: `ok`, `unprobeable` (no probe declared — common, NOT
an error), `failed` (missing tool, dead credential, gone resource — blocks
publish). Run it when a playbook misbehaves despite passing specs, or before publishing external-service
playbooks.

### CHANGING AN EXISTING WORKFLOW (a new requirement = an insertion)
A new requirement ('for EACH job role, first search LinkedIn') is almost
always an INSERTION mid-graph, NOT a step bolted on the end, NEVER a second
monolith. Recipe: read stage → find the SEAM ('for each role' means inside
the per-role loop() body) → splice the new steps there, decomposed →
RE-POINT downstream refs to the NEW step's output (this rewiring is the real
work) → validate → dry_run → fix any failed specs → publish. Never create a
'-v2' copy — edit IN PLACE by name.

### Posting to the chat from a playbook:
Steps CAN post live into the conversation the run started from:
`tool('send_chat_message', message='...')`. An `llm()`/`agent()` output is
only stored on the run record — if the owner should SEE it, a later
send_chat_message must pass it on. NEVER invent tool names — unknown tools
are rejected at authoring time.
'''


# ---- plans/014: failed-run awareness ---------------------------------------
# The agent learns about failing playbooks AMBIENTLY: prompt_sections() is
# re-read by core on every agent turn, so a conditional digest section below
# reaches the agent at the start of its next natural turn. No muted message,
# no spawned turn — the "no interrupting messages" constraint is structural.


def _rel_age(dt: datetime | None, now: datetime) -> str:
    # Server-computed relative age — the agent has no clock; never hand it
    # raw timestamps to do math on.
    if dt is None:
        return "at an unknown time"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = max(0.0, (now - dt).total_seconds())
    if secs < 90:
        return "just now"
    if secs < 90 * 60:
        return f"{int(secs // 60)} minutes ago"
    if secs < 36 * 3600:
        return f"{int(secs // 3600)} hours ago"
    return f"{int(secs // 86400)} days ago"


async def failure_digest(session) -> list[dict]:
    """Failed-run summary per enabled playbook.

    Scope: runs of the CURRENT live version only (an edit+publish resets the
    count — "since the last change"), status 'failed', gated on the
    version-scoped ack. Candidate runs are excluded by construction (their
    playbook_version is the candidate number); dry runs and spec evaluations
    never write playbook_runs rows. One grouped query over the
    (playbook_id, started_at) index; the per-playbook detail queries run only
    for playbooks that are actually failing.
    """
    from sqlalchemy import case, func, select

    from .models import Playbook, PlaybookRun, PlaybookVersion

    eff_live = func.coalesce(func.nullif(Playbook.live_version, 0), Playbook.version)
    failed = func.sum(case((PlaybookRun.status == "failed", 1), else_=0))
    finished = func.sum(
        case((PlaybookRun.status.in_(("failed", "done")), 1), else_=0)
    )
    rows = (await session.execute(
        select(
            Playbook.id,
            Playbook.name,
            eff_live.label("live"),
            failed.label("failed"),
            finished.label("finished"),
        )
        .join(
            PlaybookRun,
            (PlaybookRun.playbook_id == Playbook.id)
            & (PlaybookRun.playbook_version == eff_live)
            # 0.26.0 (plans/015, 089 §1): test runs never count as
            # production failures.
            & (PlaybookRun.is_test.is_(False)),
        )
        .where(Playbook.status == "enabled")
        .where(
            (Playbook.failures_acked_version.is_(None))
            | (Playbook.failures_acked_version != eff_live)
        )
        .group_by(Playbook.id, Playbook.name, Playbook.live_version, Playbook.version)
        .having(failed > 0)
    )).all()

    out: list[dict] = []
    for pid, name, live, n_failed, n_finished in rows:
        last = (await session.execute(
            select(PlaybookRun)
            .where(
                PlaybookRun.playbook_id == pid,
                PlaybookRun.playbook_version == live,
                PlaybookRun.status == "failed",
                PlaybookRun.is_test.is_(False),
            )
            .order_by(PlaybookRun.started_at.desc())
            .limit(1)
        )).scalars().first()
        promoted_at = (await session.execute(
            select(PlaybookVersion.created_at)
            .where(
                PlaybookVersion.playbook_id == pid,
                PlaybookVersion.version == live,
            )
            .limit(1)
        )).scalar_one_or_none()
        out.append({
            "name": name,
            "live_version": int(live),
            "failed": int(n_failed),
            "finished": int(n_finished),
            "last_failed_run_id": str(last.id) if last else None,
            "last_failed_at": last.started_at if last else None,
            "promoted_at": promoted_at,
        })
    return out


def render_failure_section(digest: list[dict], now: datetime | None = None) -> str:
    if not digest:
        return ""
    if now is None:
        now = datetime.now(timezone.utc)
    lines = ["## Playbook failures needing your attention"]
    for d in digest:
        promoted = (
            f", promoted {_rel_age(d['promoted_at'], now)}" if d["promoted_at"] else ""
        )
        lines.append(
            f"- `{d['name']}`: {d['failed']} of {d['finished']} runs FAILED "
            f"since its last change (v{d['live_version']}{promoted}). "
            f"Last failure {_rel_age(d['last_failed_at'], now)} "
            f"(run_id {d['last_failed_run_id']}, inspect with playbook_status)."
        )
    lines += [
        "",
        "You OWN these failures. First call playbook_status(run_id) to see "
        "what broke, then tell the owner in the next normal conversation "
        "turn — after finishing whatever they asked for, not instead of it. "
        "Ask what they want to do and offer: fix it (playbook_edit → "
        "publish), disable the playbook, or dismiss this notice "
        "(playbook_ack_failures). Never derail a muted or trigger turn for "
        "this. The ages above are server-computed — repeat them as given; "
        "do not do timestamp math.",
    ]
    return "\n".join(lines)


# 0.25.0 (plans/013): the delegation tools get their OWN small skill — gating
# them behind playbook-authoring would drag the ~12KB skill body into the MAIN
# conversation just to unlock the tool, defeating the context-hygiene point.
# The delegate itself receives the full authoring skill in ITS context.
_DELEGATION_SKILL_BODY = '''\
# Delegating playbook work

`playbook_agent(task, playbook="", wait_seconds=25)` hands a playbook
authoring job (create, fix, edit, add specs) to a focused background agent.
It runs the full loop — read, edit, validate, dry-run, specs, publish — in
its own context; your conversation keeps one tool call and one short result.

## When to delegate vs. do it yourself

Delegate the moment a job needs the authoring loop: creating a playbook,
fixing a failing one, changing steps, adding or repairing specs. Load
`playbook-authoring` and work inline only when the owner explicitly wants
to build it together step by step, or the change is trivial and you already
have the skill loaded this conversation.

## Phrasing the task

Write the task like a work order: goal + constraints + acceptance. Name the
playbook for edit/fix jobs. Include what the owner told you (desired
behavior, examples, the failing run's symptom). Good:
"Fix the phone format in candidate-intake: numbers must normalize to
E.164; all specs must pass; publish when green."

## After calling

A live progress card appears in the chat. If the result says `running`,
tell the owner the card below tracks the work, then END YOUR TURN. Do not
poll `playbook_agent_status` — use it only if the owner asks later. When
the result carries a report (done / failed / needs_owner), relay it in
owner words. Approval cards (e.g. publish) may appear mid-delegation —
they are the delegate asking; the owner just approves or declines.
'''


class PlaybooksPlugin(LunaPlugin):
    manifest = PluginManifest(
        name="plugin-playbooks",
        icon="workflow",
        image="assets/icon.png",
        version="0.30.2",
        description="Durable multi-step playbooks — Luna builds them, triggers fire them.",
        category="system",
        system_app=False,
        critical=False,
        depends_on=["plugin-webui"],
        routes_module="routes",
        sidebar_sections=[
            SidebarSection(
                id="playbooks",
                label="Playbooks",
                icon="workflow",
                sort_order=25,
            ),
        ],
        skills=[
            SkillDef(
                name="playbook-authoring",
                description=(
                    "how to build, edit, and debug playbooks INLINE, in this "
                    "conversation — load only when the owner asked to work "
                    "through the playbook together step by step (or told you "
                    "not to hand it off); for any other create/fix/change job "
                    "load playbook-delegation instead. The authoring tools "
                    "(propose, edit, validate, dry_run, …) unlock on your "
                    "next turn"
                ),
                body=_AUTHORING_SKILL_BODY,
                tools=[
                    "playbook_propose",
                    "playbook_edit",
                    "playbook_edit_force",
                    "playbook_manifest_set",
                    "playbook_publish",
                    "playbook_rollback",
                    "playbook_run_candidate",
                    "playbook_get_definition",
                    "playbook_validate",
                    "playbook_dry_run",
                    "playbook_set_autonomy",
                    "playbook_list_available_triggers",
                    "playbook_spec_add",
                    "playbook_spec_list",
                    "playbook_spec_delete",
                    "playbook_spec_run",
                    "playbook_spec_from_run",
                    "playbook_preflight",
                    "playbook_language_reference",
                ],
            ),
            # 0.25.0 (plans/013): small skill, big tool — see
            # _DELEGATION_SKILL_BODY for why this is not in playbook-authoring.
            SkillDef(
                name="playbook-delegation",
                description=(
                    "hand playbook work to a focused background agent with a "
                    "live progress card — the DEFAULT whenever the owner wants "
                    "a playbook created, fixed, or changed (it keeps this "
                    "conversation small); load playbook-authoring instead only "
                    "when the owner asked to build it together step by step. "
                    "playbook_agent unlocks on your next turn"
                ),
                body=_DELEGATION_SKILL_BODY,
                tools=[
                    "playbook_agent",
                    "playbook_agent_status",
                ],
            ),
        ],
    )

    def __init__(self) -> None:
        self._runner = None
        self._trigger_service = None
        self._binding_service = None
        self._session_factory = None
        self._fix_proposals = None
        self._ctx = None

    async def on_load(self, ctx: PluginContext) -> None:
        self._ctx = ctx
        self._session_factory = ctx.db_session_factory
        from .agent_tools import build_tools
        from .models import Base
        from .routes import init_routes
        from .runner import PlaybookRunner
        from .trigger_bindings import TriggerBindingService
        from .triggers import PlaybookTriggerService

        # 009.001/phase03 (E4): plugin-owned tables on the plugin's own
        # metadata — created here (idempotent), no longer by core create_all.
        async with ctx.engine.begin() as conn:
            for table in Base.metadata.sorted_tables:
                await conn.run_sync(table.create, checkfirst=True)

        # 0.8.0 (plans/002 phase 1): `table.create(checkfirst=True)` also
        # skips COLUMNS on pre-existing tables — late-added columns need an
        # ALTER (see _COLUMN_MIGRATIONS).
        try:
            await _ensure_columns(ctx.engine)
        except Exception as e:  # noqa: BLE001
            logger.warning("playbooks: column migration failed: %s", e)
        try:
            await _drop_legacy_indexes(ctx.engine)
        except Exception as e:  # noqa: BLE001
            logger.warning("playbooks: legacy index drop failed: %s", e)

        # plans/001: `table.create(checkfirst=True)` skips the whole table when
        # it already exists, indexes included — so installs that predate an
        # index never get it. Create each one on its own, and never let a
        # legacy/locked database block the plugin from loading.
        for table in Base.metadata.sorted_tables:
            for index in table.indexes:
                try:
                    async with ctx.engine.begin() as conn:
                        await conn.run_sync(index.create, checkfirst=True)
                except Exception as e:  # pragma: no cover - depends on the DB
                    logger.warning(
                        "playbooks: could not create index %s: %s", index.name, e
                    )

        self._runner = PlaybookRunner(
            session_factory=ctx.db_session_factory,
            tool_registry=ctx.tool_registry,
            events=ctx.events,
            agent=ctx.agent,
            context=ctx,
        )

        # 0.5.1: rows still "running" from before this process existed
        # (restart/upgrade, or pre-0.5.0 cancelled-mid-run coroutines) would
        # otherwise sit at "running" forever. Never block the load on it.
        try:
            await self._runner.sweep_orphaned_runs()
        except Exception as e:  # noqa: BLE001
            logger.warning("playbooks: orphan-run sweep failed: %s", e)

        # 0.8.0: backfill pblang code for pre-code playbooks. Only stored when
        # compile(codegen(ir)) reproduces the ir exactly; otherwise the code
        # stays NULL and is derived on read.
        try:
            await backfill_code(ctx.db_session_factory)
        except Exception as e:  # noqa: BLE001
            logger.warning("playbooks: code backfill failed: %s", e)

        # 0.10.0: make live_version explicit on pre-candidate rows.
        try:
            await backfill_live_version(ctx.db_session_factory)
        except Exception as e:  # noqa: BLE001
            logger.warning("playbooks: live_version backfill failed: %s", e)

        # 0.28.0 (plans/016 phase 5): specs belong to a version.
        try:
            await backfill_spec_versions(ctx.db_session_factory)
        except Exception as e:  # noqa: BLE001
            logger.warning("playbooks: spec version backfill failed: %s", e)

        init_routes(
            ctx.db_session_factory, self._runner, ctx.events,
            sync_bindings=self.sync_trigger_bindings,
            trigger_sources=ctx.trigger_sources,
            ctx=ctx,
        )

        for tool_def, handler in build_tools(
            ctx.db_session_factory, ctx.events, self._runner, ctx,
        ):
            self._register_tool(ctx, tool_def, handler)

        # 0.26.0 (plans/015, 089 §4): file fix proposals for live failures.
        from .fix_proposals import FixProposalService

        self._fix_proposals = FixProposalService(
            ctx.db_session_factory, ctx.events, ctx,
        )
        try:
            self._fix_proposals.start()
        except Exception as e:  # noqa: BLE001
            logger.warning("playbooks: fix-proposal service failed to start: %s", e)

        # 0.25.0 (plans/013): delegation tools + restart hygiene for rows a
        # dead process left at "running". Never block the load on the sweep.
        from .delegation import build_delegation_tools, sweep_orphaned_delegations

        for tool_def, handler in build_delegation_tools(
            ctx, ctx.db_session_factory, self.AUTHORING_TOOLS,
        ):
            self._register_tool(ctx, tool_def, handler)
        try:
            await sweep_orphaned_delegations(ctx.db_session_factory)
        except Exception as e:  # noqa: BLE001
            logger.warning("playbooks: orphan-delegation sweep failed: %s", e)

        self._register_trigger_tools(ctx)

        self._trigger_service = PlaybookTriggerService(
            session_factory=ctx.db_session_factory,
            events=ctx.events,
            runner=self._runner,
        )
        try:
            await self._trigger_service.start()
        except Exception:
            pass

        # 006.713: acquire/release external triggers through luna.triggers.
        self._binding_service = TriggerBindingService(
            session_factory=ctx.db_session_factory,
            registry=ctx.trigger_sources,
        )

        async def _on_playbook_changed(_payload) -> None:
            if self._trigger_service:
                await self._trigger_service.resync()
            if self._binding_service:
                await self._binding_service.sync()

        ctx.events.subscribe("playbook.saved", _on_playbook_changed)
        # Initial binding reconcile is scheduled from routes.py via a FastAPI
        # startup hook — `luna serve` boots plugins in a throwaway event loop,
        # so a task created here would die with that loop.

    async def sync_trigger_bindings(self) -> None:
        """Reconcile external trigger instances with enabled playbooks."""
        if self._binding_service is not None:
            await self._binding_service.sync()

    # 0.3.0: authoring tools ride behind the playbook-authoring skill (the
    # manifest SkillDef lists them) — building/editing playbooks is rare and
    # the skill body is required reading anyway. Run/inspect tools
    # (playbook_run/list/status/cancel) stay visible every turn. Cores
    # without the skill_gated kwarg get everything ungated.
    AUTHORING_TOOLS = (
        "playbook_propose",
        "playbook_edit",
        "playbook_edit_force",       # 0.10.0: gate the whole edit flow
        "playbook_manifest_set",
        "playbook_publish",
        "playbook_rollback",
        "playbook_run_candidate",
        "playbook_get_definition",
        "playbook_validate",
        "playbook_dry_run",
        "playbook_set_autonomy",
        "playbook_list_available_triggers",
        # 0.11.0: specs — every SkillDef tool must be here too (phase-3 rule).
        "playbook_spec_add",
        "playbook_spec_list",
        "playbook_spec_delete",
        "playbook_spec_run",
        "playbook_spec_from_run",
        # 0.12.0: preflight probes
        "playbook_preflight",
        # 0.15.0 (plans/003): on-demand language recall
        "playbook_language_reference",
    )

    # 0.25.0 (plans/013): gated by the playbook-delegation skill (its own
    # small SkillDef, NOT playbook-authoring — see _DELEGATION_SKILL_BODY).
    # Both are chat-only surfaces; the degrade-visible rule for muted turns
    # does not apply.
    DELEGATION_TOOLS = (
        "playbook_agent",
        "playbook_agent_status",
    )

    def _register_tool(self, ctx: PluginContext, tool_def, handler) -> None:
        if (
            (
                tool_def.name in self.AUTHORING_TOOLS
                or tool_def.name in self.DELEGATION_TOOLS
            )
            and getattr(ctx, "skill_registry", None) is not None
        ):
            try:
                ctx.tool_registry.register(
                    self.manifest.name, tool_def, handler, skill_gated=True
                )
                return
            except TypeError:  # older core: no skill_gated kwarg
                pass
        ctx.tool_registry.register(self.manifest.name, tool_def, handler)

    def _register_trigger_tools(self, ctx: PluginContext) -> None:
        """Agent-facing trigger discovery — reads the neutral registry."""
        from luna_sdk import ToolDef

        async def _list_available_triggers(*, app: str | None = None):
            infos = await ctx.trigger_sources.all_triggers(app)
            if not infos:
                return {
                    "triggers": [],
                    "note": (
                        "No external triggers available. Connect an app and turn on "
                        "its 'Triggers' toggle in Settings → Connectors first."
                    ),
                }
            return {
                "triggers": [
                    {
                        "event": i.event_pattern,
                        "label": i.label,
                        "app": i.app,
                        "source": i.source,
                        "description": i.description,
                    }
                    for i in infos
                ],
                "note": (
                    "Put the 'event' value in a trigger(...) entry of the playbook's "
                    "triggers=[...] list in the code you pass to playbook_propose / "
                    "playbook_edit. The trigger goes live automatically when the "
                    "playbook is saved."
                ),
            }

        self._register_tool(
            ctx,
            ToolDef(
                name="playbook_list_available_triggers",
                modes=["planning", "building", "identify", "fix_approve", "fix_publish"],
                description=(
                    "List external event triggers a playbook can bind to (from "
                    "connected apps that expose triggers — gmail, slack, github...). "
                    "Returns the exact event name to put in the playbook's "
                    "triggers=[trigger(...)] list via playbook_propose / "
                    "playbook_edit."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "app": {
                            "type": "string",
                            "description": "Optional app slug filter, e.g. 'gmail'",
                        }
                    },
                },
                policy="auto_approve",
                risk_level="low",
                timeout_seconds=60,
            ),
            _list_available_triggers,
        )

    # 089 §5: the ops chat's mode, in plain words. Keys are the contract's
    # state names; the text is what the agent reads.
    _OPS_MODE_SECTIONS = {
        "identify": (
            "## Ops chat — mode: Identify\n"
            "You are operating, not building. Monitor production playbook "
            "activity, diagnose failures, and LIST the fixes to be made as "
            "proposals for the owner to approve — a triage inbox, not a "
            "passive log. Editing, running, and publishing tools are off in "
            "this mode; suggest the owner switch the chat to a fix mode "
            "when they want changes made."
        ),
        "fix_approve": (
            "## Ops chat — mode: Fix & wait for approval\n"
            "You may edit and test fixes in a playbook's DRAFT (candidate) "
            "— production is never touched here. When a fix's test run is "
            "green, post 'fix ready — tests green, publish?' with the "
            "evidence and WAIT for the owner. The publish tool is absent in "
            "this mode on purpose: publishing happens only through the "
            "owner approving your proposal.\n"
            "The owner is not an engineer. Anything you write for them — "
            "proposals, `why` arguments, the publish `explanation` — must "
            "say in everyday language what went wrong and what the fix "
            "does, and tie back to the failure that started this work. No "
            "step ids, stack traces, or internal jargon in the summary; "
            "technical detail belongs in the collapsed section of the card."
        ),
        "fix_publish": (
            "## Ops chat — mode: Fix & publish\n"
            "You may fix and publish yourself. The publish gate is "
            "machine-checked and WILL refuse a draft with no green test run "
            "since its last edit — run the test first "
            "(playbook_run_candidate) instead of arguing with the gate. "
            "Playbooks whose publish autonomy is 'ask' still wait for the "
            "owner's approval even here.\n"
            "The publish `explanation` is read by the owner, who is not an "
            "engineer: say what problem was found, what the change does "
            "about it, and how it was tested — in everyday language, tied "
            "back to the failure that started this work. One fix, one "
            "publish, ONE approval card: batch related edits into the "
            "candidate and publish once, instead of asking per edit."
        ),
    }

    async def prompt_sections(self) -> list[str]:
        # 089 contract #6: the shipped base calls this with no args — the
        # current chat's kind/state come from the ctx accessors (None when
        # headless, which keeps the pre-0.26 rendering).
        from .publish import conversation_kind, conversation_state

        kind = conversation_kind(self._ctx)
        state = conversation_state(self._ctx)
        if not self._session_factory:
            return []

        from sqlalchemy import select
        from .models import Playbook

        async with self._session_factory() as session:
            rows = (await session.execute(
                select(
                    Playbook.name,
                    Playbook.display_name,
                    Playbook.description,
                    Playbook.when_to_use,
                ).where(Playbook.status == "enabled")
            )).all()
            # plans/014: failing-playbooks digest — same session, rendered
            # as its own section below only when non-empty.
            # A digest failure must not take down the playbook list section.
            try:
                digest = await failure_digest(session)
            except Exception:  # noqa: BLE001
                logger.exception("playbooks: failure digest query failed")
                digest = []

        sections: list[str] = []
        if kind == "ops" and (mode := self._OPS_MODE_SECTIONS.get(state or "")):
            sections.append(mode)

        if not rows:
            return sections

        lines = [
            "## Your playbooks (IMPORTANT — read carefully)",
            "Playbooks are your pre-built capabilities. They work like tools "
            "but are multi-step workflows you run with `playbook_run(name, inputs)`.",
            "",
        ]
        # 089 §5: the "MUST use it" rule is not rendered while planning
        # (nothing may change the system there) and is softened in building
        # chats; unknown kind/state (pre-089 core, headless) keeps the
        # strong rule.
        if state == "planning":
            pass
        elif kind == "building" or state == "building":
            lines += [
                "**Prefer running an existing playbook below over redoing "
                "its work manually — unless the owner is currently editing "
                "that playbook with you.**",
                "",
            ]
        elif kind != "ops":
            lines += [
                "**RULE: When a user's request matches a playbook below, you MUST "
                "use it. Do NOT do the work manually, do NOT load skills to handle "
                "it yourself, do NOT build a new workflow. The playbook already "
                "exists for this exact purpose. Just run it.**",
                "",
            ]
        for name, display_name, description, when_to_use in rows:
            parts = [p for p in [description, when_to_use] if p]
            desc = " — ".join(parts) if parts else display_name or name
            lines.append(f"- `{name}` ({display_name or name}): {desc}")

        lines += [
            "",
            "**Chat delivery**: playbook steps run in the background; an "
            "llm_step/agent_step's output goes to the run record, not the user. "
            "To surface something in the chat, a step must call the "
            "`send_chat_message` tool — live runs report into the ops chat, "
            "test runs into the chat that started them.",
        ]

        sections.append("\n".join(lines))
        # 089 §5: the failure digest is ops-chat material. It renders in the
        # ops chat and (unchanged pre-0.26 behavior) when the core doesn't
        # say which chat this is; building/planning chats stay clean.
        if kind in (None, "ops"):
            if failure_section := render_failure_section(digest):
                sections.append(failure_section)
        return sections

    async def on_unload(self) -> None:
        if self._trigger_service:
            await self._trigger_service.stop()
        if self._fix_proposals:
            self._fix_proposals.stop()
