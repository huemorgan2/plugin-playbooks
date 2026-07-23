"""plugin_playbooks — the Playbooks engine for Luna.

Provides durable, multi-step agent workflows. Playbooks are reusable
templates of steps that Luna builds through conversation and executes
on triggers or on demand.
"""

import logging

from luna_sdk import LunaPlugin, PluginContext, PluginManifest, SidebarSection, SkillDef

logger = logging.getLogger(__name__)


class PlaybooksPlugin(LunaPlugin):
    manifest = PluginManifest(
        name="plugin-playbooks",
        icon="workflow",
        image="assets/icon.png",
        version="0.4.0",
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
                body=(
                    "## Playbook Authoring\n\n"
                    "A playbook is a YAML definition with steps that execute sequentially.\n\n"
                    "### THE LOOP — build a playbook like you write code (read first)\n"
                    "Authoring a playbook IS coding. Never run blind. Always:\n"
                    "0. OUTLINE FIRST — before any YAML, write the decomposition as one "
                    "line per step: `id -> kind -> the SINGLE operation`. Then self-check "
                    "three rules: (a) any line with a quantifier (each/all/every) MUST be "
                    "a `loop`; (b) no single step may carry the whole task — if one step "
                    "would, you have one line, so keep decomposing until each line is "
                    "atomic; (c) each `agent_step`/`llm_step` is ONE judgment on ONE "
                    "thing. Only when the outline passes do you write YAML.\n"
                    "1. WRITE/EDIT the YAML (`playbook_propose` to create, "
                    "`playbook_edit` to rewrite a whole existing one).\n"
                    "2. COMPILE: `playbook_validate(name=...)` or "
                    "`playbook_validate(definition_yaml=...)` — a static check that "
                    "returns ALL errors at once (undefined {{steps}}/{{inputs}} refs, "
                    "unknown tools, bad loops, cycles). Fix every error before running.\n"
                    "3. TEST: `playbook_dry_run(name, inputs)` — simulates the run with "
                    "tool/LLM steps STUBBED. It proves loops iterate, branches pick the "
                    "right path, and templates resolve — with no side effects and no "
                    "token cost. The outputs are SIMULATED: NEVER report a dry-run value "
                    "to the user as a real result.\n"
                    "   Reading the result: `references` shows the exact template "
                    "namespace — i.e. precisely what every `steps.<id>.<field>` "
                    "resolves to. Copy your paths from there. The `trace` list is "
                    "execution order; its per-step `output` key is JUST a trace label "
                    "— do NOT write `steps.<id>.output.<field>` in templates (that key "
                    "does not exist in the namespace). A loop result is "
                    "`{iterations, results, collected}`, so gather with "
                    "`steps.<loop_id>.collected`.\n"
                    "4. RUN: `playbook_run(name, inputs)` for real.\n"
                    "5. INSPECT: `playbook_status(run_id)` — shows each step's resolved "
                    "inputs + outputs (your stack trace). Fix and repeat.\n\n"
                    "### THE POINT: turn a prompt into a process (read first)\n"
                    "A playbook's value is DECOMPOSITION — breaking a task into small, "
                    "visible, reusable steps with structured data flowing between them. "
                    "If you put the whole task into ONE agent_step's prompt ('search "
                    "emails, find subscriptions, format a table, total it'), you've "
                    "built a prompt wearing a playbook costume: one opaque LLM call, no "
                    "per-step visibility, no reuse, no typed data. Don't.\n\n"
                    "### AGENT DECIDES, THE WORKFLOW WORKS (the core principle)\n"
                    "An `agent_step` is a DECISION node — a judgment that needs tools or "
                    "memory mid-reasoning. It is NOT where you do the work. Fetching, "
                    "searching, looping, transforming, storing, traversing are WORK — "
                    "they go in `tool_call`, `loop`, `state` nodes where each is visible, "
                    "typed, and reusable. If you catch yourself writing a paragraph that "
                    "tells an agent to go DO a multi-step job, stop: that paragraph is "
                    "your step list. Call an agent only for the DECISIONS the graph "
                    "cannot express deterministically (judge / classify / rank / choose), "
                    "and put everything around the decision into explicit steps.\n"
                    "The validator enforces this and will report:\n"
                    "- `monolithic-playbook` (ERROR — blocks): the whole playbook is one "
                    "delegated step that hides a process. You must decompose it.\n"
                    "- `compound-leaf` (warning): one leaf step's prompt hides a loop or "
                    "several operations (a quantifier like 'each/all', an 'and then', or "
                    "multiple verbs). Make a loop / split the step.\n"
                    "- `agent-does-work` (warning): an `agent_step` is doing mechanical "
                    "work with no judgment — use a `tool_call`. Treat warnings as "
                    "redesign signals, not noise.\n\n"
                    "Rules of thumb:\n"
                    "- If a step's prompt contains 'and then', a numbered list, or the "
                    "word 'each', it is probably SEVERAL steps.\n"
                    "- One LLM step = ONE judgment or extraction on ONE thing "
                    "(classify THIS email; summarize THIS doc). Iterate with a `loop`, "
                    "branch with a `condition`, do deterministic work with `tool_call`.\n"
                    "- DEFAULT to `llm_step` for pure transforms (classify, extract, "
                    "summarize, format a report). It's a single raw model call — no "
                    "tools, no memory — so it's cheaper, faster (Haiku by default), and "
                    "deterministic. Use `agent_step` ONLY when the step must call tools "
                    "or use memory mid-reasoning.\n"
                    "- Prefer `output_schema` on llm/agent steps so they emit STRUCTURED "
                    "DATA (e.g. {is_subscription, service, amount}) the next step can "
                    "filter/branch on — not prose a later step has to re-parse.\n"
                    "- To gather results across a loop, use `collect` (see Loop config); "
                    "read them at `steps.<loop_id>.collected`.\n\n"
                    "### Worked example — 'scan my emails for subscriptions':\n"
                    "WRONG (one mega-step): a single agent_step whose prompt loops over "
                    "emails, classifies, formats a table, and totals it.\n"
                    "RIGHT (a process):\n"
                    "```yaml\n"
                    "steps:\n"
                    "  - id: fetch          # deterministic fetch\n"
                    "    kind: tool_call\n"
                    "    tool: gmail__gmail__fetch_emails\n"
                    "    args: {query: 'after:2024/12/15 (receipt OR invoice OR subscription)'}\n"
                    "  - id: scan           # iterate; collect one row per email\n"
                    "    kind: loop\n"
                    "    over: '{{ steps.fetch.result.messages }}'  # tool data is under .result\n"
                    "    item_name: email\n"
                    "    collect: '{{ steps.classify }}'\n"
                    "    body:\n"
                    "      - id: classify   # ONE judgment on ONE email, structured out\n"
                    "        kind: llm_step  # raw model call — no tools needed\n"
                    "        output_schema: {is_subscription: bool, service: str, amount: number}\n"
                    "        prompt: 'Is THIS ONE email a paid subscription? {{ email }}'\n"
                    "  - id: report         # consume the collected rows, filter, format\n"
                    "    kind: llm_step      # pure formatting → Haiku, no tools\n"
                    "    output_schema: {report: str}\n"
                    "    prompt: |\n"
                    "      Build a markdown report of subscriptions from these rows:\n"
                    "      {{ steps.scan.collected | selectattr('is_subscription') | list }}\n"
                    "  - id: notify         # surface it in chat\n"
                    "    kind: tool_call\n"
                    "    tool: send_chat_message\n"
                    "    args: {message: '{{ steps.report.report }}'}\n"
                    "```\n"
                    "Each step is inspectable on the canvas, `classify` is reusable, and "
                    "the data between steps is typed. THAT is a playbook.\n\n"
                    "### CONTEXT ECONOMY — iterate, never dump (critical)\n"
                    "Keep the AGENTIC CONTEXT small. The #1 way a playbook fails is "
                    "dumping a big collection into ONE model call and exploding the "
                    "context window. To process N items (emails, rows, docs, search "
                    "results):\n"
                    "- LOOP over them; read/summarize ONE per iteration with an "
                    "`llm_step`; emit a SMALL structured result per item; `collect` it.\n"
                    "- Then operate on the reduced set (filter/aggregate/format), or "
                    "persist each item to a store/DB and query it later.\n"
                    "- NEVER write a single step whose prompt interpolates a whole "
                    "collection like `{{ steps.fetch.result.messages }}` for 1000 emails "
                    "— that is brute force and will fail. The validator warns when it "
                    "sees this; treat that warning as a redesign signal. Iteration beats "
                    "brute force, always.\n\n"
                    "### REFERENCE SHAPES — get the path right or the run fails LOUD\n"
                    "Templates now fail loudly on an undefined reference (no more silent "
                    "nulls). Two shapes trip everyone up — memorize them:\n"
                    "- `tool_call` output is wrapped: read the tool's data under "
                    "`.result`. A tool returning {messages: [...]} is "
                    "`steps.<id>.result.messages` — NOT `steps.<id>.messages`.\n"
                    "- A schemaless `llm_step`/`agent_step` returns `{_raw: <text>}`. "
                    "Read it as `steps.<id>._raw`. There is NO `.output`. To get typed "
                    "fields (`steps.<id>.field`), declare an `output_schema`. This is the "
                    "#1 cause of a loop that collected nulls — `collect` an "
                    "`output_schema` field, or `._raw`, never `.output`.\n"
                    "- `loop` output is {iterations, results, collected, stopped} — gather "
                    "with `steps.<loop_id>.collected`. `stopped` is null (drained), "
                    "'break' (break_when), or 'max_iterations' (hit the cap).\n"
                    "The validator checks these shapes statically — if it errors on a "
                    "`.field`, you have the wrong path; copy the right one from "
                    "`dry_run`'s `references`.\n\n"
                    "### RUN-SCOPED STATE — stacks, queues, sets, counters (the big one)\n"
                    "A `state` step mutates run-scoped variables that PERSIST across loop "
                    "iterations. Read them in any template as `vars.<name>` (note: "
                    "`vars.items` reads the KEY `items`, it is safe to name a queue "
                    "`items`/`keys`/`values`). This is how you build a REAL recursive "
                    "crawl / BFS / DFS / dedup / accumulator — instead of HARDCODING a "
                    "list of things you guessed.\n"
                    "Ops (one `state` step may carry several, applied in order):\n"
                    "- `set` var=value | `append`/`extend` (list grow) | `merge` (dict)\n"
                    "- `push_back` + `pop_back` = STACK (LIFO)\n"
                    "- `push_back` + `pop_front` = QUEUE (FIFO)\n"
                    "- `pop_back`/`pop_front` need `into: <var>` to capture what you "
                    "popped (else it's discarded — the validator warns)\n"
                    "- `add_unique` = SET (dedup) | `incr`/`decr` = COUNTER | `delete`\n"
                    "`value` is a Jinja expression when it's a string: "
                    "`value: \"[ inputs.start ]\"`, `value: \"{{ vars.url }}\"`, "
                    "`value: \"[]\"`, `value: \"1\"`.\n"
                    "Loops gained `while:` (loop WHILE truthy — the frontier pattern), "
                    "`break_when:` (stop after an iteration), and `concurrency: N` "
                    "(bounded parallel map; the body must NOT mutate shared state).\n\n"
                    "### NEVER HARDCODE A DISCOVERABLE LIST (hard rule)\n"
                    "If a task is 'scan/crawl/traverse a site / a tree / paginated "
                    "results / a graph', you MUST discover items at RUN TIME with a "
                    "frontier — do NOT write N sibling tool_calls to URLs/items you "
                    "guessed or found yourself. Hand-listing items a loop could fetch is a "
                    "junior mistake and the validator flags it. The pattern:\n"
                    "```yaml\n"
                    "steps:\n"
                    "  - id: seed\n"
                    "    kind: state\n"
                    "    state:\n"
                    "      - { op: set, var: frontier, value: '[ inputs.start_url ]' }\n"
                    "      - { op: set, var: visited, value: '[]' }\n"
                    "  - id: crawl\n"
                    "    kind: loop\n"
                    "    while: '{{ vars.frontier | length > 0 }}'   # grows + shrinks\n"
                    "    max_iterations: 200                          # safety cap\n"
                    "    body:\n"
                    "      - id: take\n"
                    "        kind: state\n"
                    "        state:\n"
                    "          - { op: pop_front, var: frontier, into: cur }  # FIFO = BFS\n"
                    "          - { op: add_unique, var: visited, value: '{{ vars.cur }}' }\n"
                    "      - id: fetch\n"
                    "        kind: tool_call\n"
                    "        tool: web_fetch\n"
                    "        args: { url: '{{ vars.cur }}' }\n"
                    "      - id: links            # extract links from THIS page only\n"
                    "        kind: llm_step\n"
                    "        output_schema: { links: array }\n"
                    "        prompt: 'List internal link URLs on this page:\\n"
                    "{{ steps.fetch.result }}'\n"
                    "      - id: enqueue          # add unseen links to the frontier\n"
                    "        kind: loop\n"
                    "        over: '{{ steps.links.links }}'\n"
                    "        item_name: link\n"
                    "        body:\n"
                    "          - id: gate\n"
                    "            kind: condition\n"
                    "            when: '{{ link not in vars.visited and link not in vars.frontier }}'\n"
                    "            then:\n"
                    "              - id: push\n"
                    "                kind: state\n"
                    "                state: [ { op: push_back, var: frontier, value: '{{ link }}' } ]\n"
                    "```\n"
                    "Swap `pop_front`→`pop_back` for DFS. The `visited` set makes it "
                    "cycle-safe; `max_iterations` bounds it. THAT is a crawl.\n\n"
                    "### Step kinds:\n"
                    "- `tool_call`: calls a registered Luna tool with templated args\n"
                    "- `llm_step`: a RAW model call (no tools/memory/identity) — PREFER "
                    "this for transforms. Config: `prompt` (required), `output_schema` "
                    "(structured out), `purpose` (router chain; default `summarization` "
                    "→ Haiku; use `reasoning` for the big model), `model` "
                    "(\"provider/model\" to force one), `system` (optional system text). "
                    "Returns a structured dict (with output_schema) or `{_raw: text}`.\n"
                    "- `agent_step`: FULL agent turn via run_turn() — system prompt, tool "
                    "catalog, memory, skills. Use ONLY when the step needs tools/memory "
                    "mid-reasoning; otherwise use `llm_step`. Returns structured dict.\n"
                    "- `condition`: branches on a Jinja expression (then/else)\n"
                    "- `parallel`: fan-out N branches, fan-in waits for all\n"
                    "- `wait_for_approval`: pauses, resumes on owner click\n"
                    "- `wait_for_event`: pauses, resumes on matching bus event\n"
                    "- `subtask`: invokes another playbook with mapped inputs; add "
                    "`returns: {key: '{{ steps.<sub_id>.field }}'}` to surface the "
                    "sub-workflow's outputs to the parent as steps.<subtask_id>.key\n"
                    "- `loop`: repeats body `over` a list, `while`/`until` a condition; "
                    "supports `break_when` and `concurrency`\n"
                    "- `state`: mutate run-scoped `vars` (stack/queue/set/counter/dict) — "
                    "see RUN-SCOPED STATE above\n"
                    "- `halt`: end the run early as SUCCESS (optional `when:` guard, "
                    "optional `value:` result) — e.g. stop when nothing to do\n\n"
                    "### Trigger syntax:\n"
                    "```yaml\n"
                    "triggers:\n"
                    "  - event: email.received\n"
                    "    filter: {label: 'support'}\n"
                    "    map: {email: '{{event.payload}}'}\n"
                    "```\n\n"
                    "### Templates:\n"
                    "Use Jinja2: `{{inputs.email.body}}`, `{{steps.classify.class}}`\n\n"
                    "### Creating a new playbook (whole-YAML — the ONLY way):\n"
                    "Write the COMPLETE YAML (steps, triggers, inputs) and call "
                    "`playbook_propose(name, definition_yaml=...)`. Author the whole "
                    "definition at once like a source file — do NOT build a playbook "
                    "node by node. There are no add-step / add-trigger / save tools; "
                    "everything is one YAML document. Validate first with "
                    "`playbook_validate(definition_yaml=...)` if unsure.\n\n"
                    "### Loop config (exact syntax — no other fields work):\n"
                    "```json\n"
                    "{\"over\": \"range(1, inputs.n + 1)\", \"item_name\": \"number\", \"max_iterations\": 100}\n"
                    "```\n"
                    "- `over`: a LITERAL LIST (e.g. `[1, 2, 3]`) or a Jinja expression "
                    "producing a list (e.g. `\"range(1, inputs.n + 1)\"`)\n"
                    "- `until`: alternative — Jinja condition, loops UNTIL true\n"
                    "- `while`: loops WHILE true (the frontier/queue pattern; mutate a "
                    "`vars.*` each iteration with a state step or it runs to the cap)\n"
                    "- `break_when`: Jinja condition checked AFTER each iteration; stops "
                    "the loop early (result `stopped: 'break'`)\n"
                    "- `concurrency: N`: run up to N item bodies in parallel (default 1). "
                    "Bodies are isolated — do NOT mutate shared state inside a "
                    "concurrent loop; only `collect` merges back (in item order). "
                    "PREFER `concurrency: 4` (or the item count if smaller) whenever "
                    "the body is side-effect-free — pure reads, per-item LLM calls, "
                    "fetches; sequential `over` loops waste wall-clock. Keep "
                    "`concurrency: 1` only when the body mutates `vars.*`/shared "
                    "state or when ordering between items matters\n"
                    "- `max_iterations`: hard safety cap; on hit, result `stopped: "
                    "'max_iterations'` (always set one on a `while` loop)\n"
                    "- `count: N` is accepted as shorthand for `over: range(1, N + 1)`\n"
                    "- `item_name`: name for the current item inside the body — "
                    "`item_name: \"number\"` makes `{{ number }}` and `{{ number_index }}` work\n"
                    "- `collect`: a Jinja expression evaluated AFTER each iteration's "
                    "body (item vars still in scope); each result is appended to a list "
                    "exposed as `{{ steps.<loop_id>.collected }}`. THIS is how you gather "
                    "per-iteration outputs — without it, only the last iteration's step "
                    "outputs survive the loop. Collect everything, then filter in the "
                    "next step (e.g. `| selectattr('is_subscription')`).\n"
                    "- `{{steps.<loop_id>._item}}` and `{{steps.<loop_id>._index}}` also work — "
                    "there is NO `loop.index`\n"
                    "- Keys like `iterator`, `from`, `to` DO NOT EXIST — unknown keys are rejected\n"
                    "- Undefined variables in templates or `over`/`until` FAIL the run loudly\n"
                    "- A loop with an empty body does NOTHING — you MUST nest steps inside it.\n\n"
                    "### Nesting steps inside loops/conditions:\n"
                    "Nesting is pure YAML structure. Put child steps under the parent's "
                    "`body:` (loop) or `then:`/`else:` (condition) keys — see the loop "
                    "and crawl examples above. A loop with an empty body does nothing, "
                    "so always nest at least one step inside it.\n\n"
                    "### CHANGING AN EXISTING WORKFLOW (a new requirement = an insertion)\n"
                    "A new requirement (e.g. 'for EACH job role, first search LinkedIn "
                    "for comparables and make a list') is almost always an INSERTION "
                    "mid-graph, NOT a step bolted on the end, and NEVER a second "
                    "monolith. Recipe:\n"
                    "1. `playbook_get_definition(name)` — read the current YAML.\n"
                    "2. Find the SEAM — where the new work belongs. 'for each role' means "
                    "inside the per-role `loop` body (before whatever consumes the role), "
                    "not a new top-level step.\n"
                    "3. Splice the new steps there, decomposed (a quantifier -> a loop; "
                    "one judgment per item; collect). \n"
                    "4. RE-POINT downstream refs — the steps that ran after the seam must "
                    "now read the NEW step's output (e.g. the ranking step now also reads "
                    "`steps.<comparables>.collected`). This rewiring is the real work of "
                    "a change.\n"
                    "5. `playbook_validate` -> `playbook_dry_run` -> `playbook_run`. The "
                    "same lints apply, so the insertion can't reintroduce a monolith.\n\n"
                    "### Editing an existing playbook (whole-YAML, version history):\n"
                    "To modify a live playbook, edit it IN PLACE by NAME — NEVER create a "
                    "new playbook (no '-v2' copies).\n\n"
                    "1. `playbook_get_definition(name)` → read the current full YAML.\n"
                    "2. Edit that YAML — make ALL your changes to the whole document.\n"
                    "3. `playbook_edit(name, definition_yaml=...)` → it snapshots a "
                    "version, validates, and replaces the definition in one step, the "
                    "same way you'd save an edited source file.\n"
                    "There is NO incremental/node-by-node edit path — always rewrite the "
                    "whole YAML. After editing, re-run `playbook_validate` and "
                    "`playbook_dry_run`.\n\n"
                    "### Posting to the chat from a playbook (006.712):\n"
                    "- Steps CAN post messages into the chat: call the `send_chat_message` "
                    "tool (via a `tool_call` step with args `{\"message\": \"...\"}`, or "
                    "instruct an `agent_step` to call it). Messages land in the "
                    "conversation the run was started from, live.\n"
                    "- An `llm_step`/`agent_step` output is only stored on the run record — "
                    "if the owner should SEE something, a later `tool_call` step must "
                    "pass it to `send_chat_message` (an `llm_step` can't call tools).\n"
                    "- NEVER invent other tool names. A `tool_call` step must reference a tool "
                    "from your actual tool list — unknown tools are rejected at authoring time."
                ),
                tools=[
                    "playbook_propose",
                    "playbook_edit",
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
