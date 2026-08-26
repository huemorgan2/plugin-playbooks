"""plugin_playbooks — the Playbooks engine for Luna.

Provides durable, multi-step agent workflows. Playbooks are reusable
templates of steps that Luna builds through conversation and executes
on triggers or on demand.
"""

import logging

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

A playbook is written in playbook CODE — a restricted Python dialect. You WRITE
code; Luna PARSES and COMPILES it into a step graph. It is NEVER executed as
Python: no imports, no def/class, no for/while/if statements, no function calls
except the combinators below and range(). Each statement is one step.

### THE LOOP — build a playbook like you write code (read first)
Authoring a playbook IS coding. Never run blind. Always:
0. OUTLINE FIRST — before any code, write the decomposition as one line per
step: `id -> kind -> the SINGLE operation`. Then self-check three rules:
(a) any line with a quantifier (each/all/every) MUST be a `loop`; (b) no
single step may carry the whole task — if one step would, you have one line,
so keep decomposing until each line is atomic; (c) each `agent()`/`llm()` is
ONE judgment on ONE thing. Only when the outline passes do you write code.
1. WRITE: `playbook_propose(name, code=...)` to create. EDIT: two steps —
`playbook_edit(name)` alone first (returns the MANIFEST, the current code, and
a single-use edit ticket), then `playbook_edit(name, ticket=..., code=...)`
with the full new source, or `playbook_edit(name, ticket=..., old=..., new=...)`
for a targeted change (the `old` snippet must match exactly one place). See
MANIFEST + THE EDIT FLOW below.
2. COMPILE: `playbook_validate(code=...)` or `playbook_validate(name=...)` —
returns ALL errors at once (compile errors with line numbers, undefined
steps/inputs references, unknown tools, bad loops, cycles). Fix every error.
3. TEST: `playbook_dry_run(name, inputs)` — simulates the run with tool/LLM
steps STUBBED. It proves loops iterate, branches pick the right path, and
templates resolve — no side effects, no token cost. When a candidate exists it
tests the CANDIDATE by default (`version='live'` overrides). Outputs are
SIMULATED: NEVER report a dry-run value to the user as a real result.
   Reading the result: `references` shows the exact template namespace — i.e.
precisely what every `steps.<id>.<field>` resolves to. Copy your paths from
there. The `trace` list is execution order; its per-step `output` key is JUST
a trace label — do NOT write `steps.<id>.output.<field>` (that key does not
exist). A loop result is `{iterations, results, collected}`, so gather with
`steps.<loop_id>.collected`.
4. RUN: `playbook_run(name, inputs)` for real. Runs execute in the background:
fast playbooks return results directly; if the result says status 'running',
poll `playbook_status(run_id)` until 'done'/'failed'. Never re-run a 'running'
playbook and never invent its results.
5. INSPECT: `playbook_status(run_id)` — each step's resolved inputs + outputs
(your stack trace). Fix and repeat.

### THE POINT: turn a prompt into a process (read first)
A playbook's value is DECOMPOSITION — breaking a task into small, visible,
reusable steps with structured data flowing between them. If you put the whole
task into ONE agent() prompt ('search emails, find subscriptions, format a
table, total it'), you've built a prompt wearing a playbook costume: one opaque
LLM call, no per-step visibility, no reuse, no typed data. Don't.

### AGENT DECIDES, THE WORKFLOW WORKS (the core principle)
An `agent()` step is a DECISION node — a judgment that needs tools or memory
mid-reasoning. It is NOT where you do the work. Fetching, searching, looping,
transforming, storing, traversing are WORK — they go in `tool()`, `loop()`,
`state()` steps where each is visible, typed, and reusable. If you catch
yourself writing a paragraph that tells an agent to go DO a multi-step job,
stop: that paragraph is your step list. Call an agent only for the DECISIONS
the graph cannot express deterministically (judge / classify / rank / choose).
The validator enforces this and will report:
- `monolithic-playbook` (ERROR — blocks): the whole playbook is one delegated
step that hides a process. You must decompose it.
- `compound-leaf` (warning): one leaf step's prompt hides a loop or several
operations. Make a loop / split the step.
- `agent-does-work` (warning): an `agent()` doing mechanical work with no
judgment — use a `tool()`. Treat warnings as redesign signals, not noise.

Rules of thumb:
- If a step's prompt contains 'and then', a numbered list, or the word 'each',
it is probably SEVERAL steps.
- One LLM step = ONE judgment or extraction on ONE thing (classify THIS email).
Iterate with `loop()`, branch with `if_()`, do deterministic work with `tool()`.
- DEFAULT to `llm()` for pure transforms (classify, extract, summarize,
format). It's a single raw model call — no tools, no memory — cheaper, faster
(Haiku by default), deterministic. Use `agent()` ONLY when the step must call
tools or use memory mid-reasoning.
- Prefer `output=` on llm/agent steps so they emit STRUCTURED DATA the next
step can filter/branch on — not prose a later step has to re-parse.
- To gather results across a loop, use `collect=`; read them at
`steps.<loop_id>.collected`.

### Worked example — 'scan my emails for subscriptions':
WRONG (one mega-step): a single agent() whose prompt loops over emails,
classifies, formats a table, and totals it.
RIGHT (a process):
```python
playbook(
    name='subscription-scan',
    description='Scan emails for paid subscriptions',
    when_to_use='When the owner asks what subscriptions they pay for',
)

fetch = tool('gmail__gmail__fetch_emails', query='after:2024/12/15 (receipt OR invoice)')
scan = loop(
    over='{{ steps.fetch.result.messages }}',
    item_name='email',
    concurrency=4,
    body=[
        llm(
            'Is THIS ONE email a paid subscription? {{ email }}',
            output={'is_subscription': 'bool', 'service': 'str', 'amount': 'number'},
            id='classify',
        ),
    ],
    collect='{{ steps.classify }}',
)
report = llm(
    """Build a markdown report of subscriptions from:
{{ steps.scan.collected | selectattr('is_subscription') | list }}""",
    output={'report': 'str'},
)
notify = tool('send_chat_message', message='{{ steps.report.report }}')
```
Each step is inspectable on the canvas, `classify` is reusable, and the data
between steps is typed. THAT is a playbook.

### THE LANGUAGE — exact rules (the compiler enforces all of this)
- The FIRST statement must be the `playbook(...)` header: `name=` (required),
`display_name=`, `description=`, `when_to_use=`, `agent_autonomy=`,
`inputs=` (JSON-schema dict), `triggers=[trigger(event=..., filter={...},
map={...})]`.
- Every step is `<id> = combinator(...)`. The VARIABLE NAME is the step id —
later steps reference it as `steps.<id>....`. Inside nested lists (loop body,
then/else, parallel branches) use walrus `(x := llm(...))` or pass `id='x'`.
A bare call with no name gets a generated id.
- Combinators (the ONLY callables, plus range()):
  - `tool('tool_name', **args)` — call a registered Luna tool; loose kwargs
    are the tool args.
  - `llm(prompt, output={...}, purpose=..., model=..., system=...)` — raw
    model call, no tools. `purpose='reasoning'` routes to the big model.
  - `agent(prompt, output={...}, tools=[...])` — full agent turn.
  - `if_(cond, then=[...], else_=[...])` — branch.
  - `loop(over=... | while_=... | until=..., item_name=..., body=[...],
    collect=..., break_when=..., max_iterations=..., concurrency=...)`.
  - `parallel([[...], [...]], fan_in='all')` — list of branch step-lists.
  - `approve(show=[...])` — pause for owner approval.
  - `wait_event('event.name', filter={...}, timeout_seconds=...)`.
  - `subtask('other-playbook', inputs={...}, returns={...})`.
  - `state(op, op, ...)` — mutate run vars; ops below.
  - `halt(when=..., value=...)` — end the run early as SUCCESS.
- Common kwargs on ANY step: `id=`, `explanation=`, `retry=` (int, or
`{'max': 2, 'backoff_seconds': 5.0}`), `on_error=` ('abort'|'continue'),
`timeout=` seconds.
- EXPRESSIONS: plain strings pass through verbatim — write Jinja `{{ ... }}`
inside them for templating. Bare Python expressions over `inputs`, `vars`,
`steps`, `event` also work (`inputs.n + 1`, `steps.fetch.result`), and
f-strings work in prompts/args. Jinja FILTERS (`| length`, `| selectattr`)
only exist inside strings: write `'{{ vars.frontier | length > 0 }}'`.
- BANNED (compile errors with a hint): any other function call, comprehensions,
lambdas, imports, def/class, Python for/while/if statements (use loop()/if_()),
`is`/`is not`. Duplicate step ids are rejected.
- The `name` in the header cannot rename an existing playbook — the tool's
`name` argument always wins.

### CONTEXT ECONOMY — iterate, never dump (critical)
Keep the AGENTIC CONTEXT small. The #1 way a playbook fails is dumping a big
collection into ONE model call. To process N items: LOOP over them, judge ONE
per iteration with `llm()`, emit a SMALL structured result, `collect=` it, then
operate on the reduced set. NEVER write a single step whose prompt interpolates
a whole collection like `{{ steps.fetch.result.messages }}` for 1000 emails.
The validator warns on this; treat it as a redesign signal.

### REFERENCE SHAPES — get the path right or the run fails LOUD
Templates fail loudly on an undefined reference. Memorize these:
- `tool()` output is wrapped: read the tool's data under `.result`. A tool
returning {messages: [...]} is `steps.<id>.result.messages`.
- A schemaless `llm()`/`agent()` returns `{_raw: <text>}` — read
`steps.<id>._raw`. There is NO `.output`. To get typed fields declare
`output=`. This is the #1 cause of a loop that collected nulls.
- `loop()` output is {iterations, results, collected, stopped} — gather with
`steps.<loop_id>.collected`. `stopped` is null (drained), 'break'
(break_when), or 'max_iterations' (hit the cap).
If validate errors on a `.field`, you have the wrong path; copy the right one
from `dry_run`'s `references`.

### RUN-SCOPED STATE — stacks, queues, sets, counters (the big one)
A `state()` step mutates run-scoped variables that PERSIST across loop
iterations. Read them anywhere as `vars.<name>`. This is how you build a REAL
recursive crawl / BFS / DFS / dedup / accumulator — instead of HARDCODING a
list you guessed. Ops (one state() may carry several, applied in order):
- `set_('var', value)` | `append`/`extend` (list grow) | `merge` (dict)
- `push_back` + `pop_back` = STACK (LIFO)
- `push_back` + `pop_front` = QUEUE (FIFO)
- `pop_back('var', into='x')` / `pop_front('var', into='x')` — `into=`
captures what you popped (else it's discarded — the validator warns)
- `add_unique` = SET (dedup) | `incr('n')`/`decr('n')` = COUNTER | `delete`
Values are expressions: `set_('frontier', '[ inputs.start_url ]')`,
`push_back('frontier', '{{ link }}')`, `set_('n', '0')`.

### NEVER HARDCODE A DISCOVERABLE LIST (hard rule)
If a task is 'scan/crawl/traverse a site / a tree / paginated results /
a graph', you MUST discover items at RUN TIME with a frontier — do NOT write N
sibling tool() calls to URLs/items you guessed. The pattern:
```python
playbook(
    name='site-crawl',
    description='BFS crawl',
    inputs={'type': 'object', 'properties': {'start_url': {'type': 'string'}}},
)

seed = state(set_('frontier', '[ inputs.start_url ]'), set_('visited', '[]'))
crawl = loop(
    while_='{{ vars.frontier | length > 0 }}',   # frontier grows + shrinks
    max_iterations=200,                          # safety cap
    body=[
        state(
            pop_front('frontier', into='cur'),   # FIFO = BFS
            add_unique('visited', '{{ vars.cur }}'),
            id='take',
        ),
        tool('web_fetch', url='{{ vars.cur }}', id='fetch'),
        llm(
            """List internal link URLs on this page:
{{ steps.fetch.result }}""",
            output={'links': 'array'},
            id='links',
        ),
        loop(
            over='{{ steps.links.links }}',
            item_name='link',
            body=[
                if_(
                    '{{ link not in vars.visited and link not in vars.frontier }}',
                    then=[
                        state(push_back('frontier', '{{ link }}'), id='push'),
                    ],
                    id='gate',
                ),
            ],
            id='enqueue',
        ),
    ],
)
```
Swap pop_front→pop_back for DFS. The `visited` set makes it cycle-safe;
`max_iterations` bounds it. THAT is a crawl.

### Loop config (exact kwargs — no other fields work):
- `over=`: a literal list or an expression producing one
(`over='{{ range(1, inputs.n + 1) }}'` or `over=[1, 2, 3]`)
- `until=`: loops UNTIL the condition is true
- `while_=`: loops WHILE true (the frontier pattern; mutate a `vars.*` each
iteration with a state() step or it runs to the cap — ALWAYS set
`max_iterations` on a while loop)
- `break_when=`: checked AFTER each iteration; stops early (stopped: 'break')
- `concurrency=N`: run up to N item bodies in parallel (default 1). Bodies are
isolated — do NOT mutate shared state inside a concurrent loop; only
`collect` merges back (in item order). PREFER `concurrency=4` whenever the
body is side-effect-free; sequential `over` loops waste wall-clock.
- `item_name='email'` makes `{{ email }}` and `{{ email_index }}` available
inside the body
- `collect=`: an expression evaluated AFTER each iteration (item vars still in
scope); results append to `steps.<loop_id>.collected`. THIS is how you gather
per-iteration outputs — without it only the last iteration survives.
- A loop with an empty body does NOTHING — nest at least one step in `body=[]`.

### MANIFEST + THE EDIT FLOW (read → ticket → write)
Every playbook can carry a MANIFEST: the owner's intent in plain markdown —
Purpose, Side effects, Never (invariants), Acceptance. Editing is a TWO-STEP
flow enforced by the tools:
1. READ: `playbook_edit(name)` with nothing else → returns `{manifest, code,
version, ticket, expires_in_seconds}`. Read the manifest — your edit must stay
within it.
2. WRITE: `playbook_edit(name, ticket=..., ...)` with exactly one of `code=`,
`old=`/`new=`, or `definition_yaml=`. The ticket is single-use and expires in
15 minutes; a save without a valid ticket is refused.
Every write is checked against the manifest. If it conflicts you get the
reason and three legal moves: fix the code and retry with the SAME ticket
(a refusal does not burn it); or update the manifest via
`playbook_manifest_set(name, manifest)` (asks the owner); or, if the owner
explicitly wants the conflicting change, `playbook_edit_force` (also asks the
owner). NEVER work around a manifest refusal any other way.
When you create a playbook, pass `manifest=` to `playbook_propose` — a few
short lines stating purpose, side effects, and what must never happen. If a
playbook has none, propose one to the owner via `playbook_manifest_set`.

### CANDIDATE → PROMOTE (a save never changes the running playbook)
Saving an edit creates a CANDIDATE version — the LIVE playbook keeps running
unchanged (triggers, `playbook_run`) until you promote. The loop:
1. `playbook_edit` (two-step, above) → `{status: 'candidate_saved',
candidate_version, live_version}`. Editing again iterates on the candidate
(the read stage hands out candidate code; one candidate max — a new save
replaces the pointer, history keeps every version).
2. `playbook_dry_run(name, inputs)` — tests the candidate by default.
3. Real proof, if the owner wants it: `playbook_run_candidate(name, inputs)` —
a REAL supervised run of the candidate (side effects included; asks the owner).
4. `playbook_promote(name)` — runs the promotion gate (static validation;
manifest drift was enforced at save time) and makes the candidate live. A
refusal names the failing gate — fix the candidate, never bypass the gate.
5. If a promoted change misbehaves: `playbook_rollback(name)` restores the
previous live version.
NEVER report an edit as done after `candidate_saved` — the owner's playbook
still runs the old version until promote succeeds.

### CHANGING AN EXISTING WORKFLOW (a new requirement = an insertion)
A new requirement (e.g. 'for EACH job role, first search LinkedIn for
comparables') is almost always an INSERTION mid-graph, NOT a step bolted on the
end, and NEVER a second monolith. Recipe:
1. `playbook_edit(name)` — the read stage: manifest + current code + ticket.
2. Find the SEAM — 'for each role' means inside the per-role loop() body, not
a new top-level step.
3. Splice the new steps there, decomposed. Use
`playbook_edit(ticket=..., old=..., new=...)` for a surgical splice, or
`playbook_edit(ticket=..., code=...)` with the full new source.
4. RE-POINT downstream refs — steps after the seam must now read the NEW
step's output. This rewiring is the real work of a change.
5. `playbook_validate` -> `playbook_dry_run` (candidate) -> `playbook_promote`
-> `playbook_run`.
Never create a '-v2' copy — edit IN PLACE by name; every version is kept in
history and `playbook_rollback` restores the previous live one.

### Posting to the chat from a playbook:
- Steps CAN post into the chat: `tool('send_chat_message', message='...')`.
Messages land in the conversation the run was started from, live.
- An `llm()`/`agent()` output is only stored on the run record — if the owner
should SEE something, a later `tool('send_chat_message', ...)` must pass it on.
- NEVER invent tool names. A `tool()` step must reference a tool from your
actual tool list — unknown tools are rejected at authoring time.
- Legacy: `definition_yaml=` (the raw YAML IR) is still accepted by
propose/edit/validate, and `playbook_get_definition(name, format='yaml')`
returns it. Prefer code.
'''


class PlaybooksPlugin(LunaPlugin):
    manifest = PluginManifest(
        name="plugin-playbooks",
        icon="workflow",
        image="assets/icon.png",
        version="0.10.0",
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
                    "how to build, edit, and debug playbooks — load before creating or "
                    "modifying any playbook; the authoring tools (propose, edit, "
                    "validate, dry_run, …) unlock on your next turn"
                ),
                body=_AUTHORING_SKILL_BODY,
                tools=[
                    "playbook_propose",
                    "playbook_edit",
                    "playbook_edit_force",
                    "playbook_manifest_set",
                    "playbook_promote",
                    "playbook_rollback",
                    "playbook_run_candidate",
                    "playbook_get_definition",
                    "playbook_validate",
                    "playbook_dry_run",
                    "playbook_set_autonomy",
                    "playbook_list_available_triggers",
                ],
            ),
        ],
    )

    def __init__(self) -> None:
        self._runner = None
        self._trigger_service = None
        self._binding_service = None
        self._session_factory = None

    async def on_load(self, ctx: PluginContext) -> None:
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

        init_routes(
            ctx.db_session_factory, self._runner, ctx.events,
            sync_bindings=self.sync_trigger_bindings,
        )

        for tool_def, handler in build_tools(
            ctx.db_session_factory, ctx.events, self._runner,
        ):
            self._register_tool(ctx, tool_def, handler)

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
        "playbook_promote",
        "playbook_rollback",
        "playbook_run_candidate",
        "playbook_get_definition",
        "playbook_validate",
        "playbook_dry_run",
        "playbook_set_autonomy",
        "playbook_list_available_triggers",
    )

    def _register_tool(self, ctx: PluginContext, tool_def, handler) -> None:
        if (
            tool_def.name in self.AUTHORING_TOOLS
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
                    "Put the 'event' value in the playbook's `triggers:` block in the "
                    "YAML you pass to playbook_propose / playbook_edit. The trigger "
                    "goes live automatically when the playbook is saved."
                ),
            }

        self._register_tool(
            ctx,
            ToolDef(
                name="playbook_list_available_triggers",
                description=(
                    "List external event triggers a playbook can bind to (from "
                    "connected apps that expose triggers — gmail, slack, github...). "
                    "Returns the exact event name to put in the playbook's "
                    "`triggers:` block via playbook_propose / playbook_edit."
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

    async def prompt_sections(self) -> list[str]:
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

        if not rows:
            return []

        lines = [
            "## Your playbooks (IMPORTANT — read carefully)",
            "Playbooks are your pre-built capabilities. They work like tools "
            "but are multi-step workflows you run with `playbook_run(name, inputs)`.",
            "",
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
            "`send_chat_message` tool — it posts into the conversation the "
            "run was started from, live.",
        ]

        return ["\n".join(lines)]

    async def on_unload(self) -> None:
        if self._trigger_service:
            await self._trigger_service.stop()
