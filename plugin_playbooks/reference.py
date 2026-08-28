"""plans/003 phase 4: the compact playbook-language reference.

The full spec lives in the authoring skill, which is loaded once and can fall
out of a long session's context — after which agents guess syntax and burn
edit→validate→dry_run cycles probing. This sheet is the recall surface: it
rides on failed validate/propose/compile results and behind the
playbook_language_reference tool. Keep it COMPLETE on facts (every
combinator, every filter) and short on prose.

The playbook_edit READ stage carries only LANGUAGE_MINIREF (012 phase 3):
the step kinds, the reference shapes, and the rules agents actually forget,
plus a pointer to the full sheet — the ~5KB body stops riding on every edit.
"""

LANGUAGE_CHEATSHEET = """\
PLAYBOOK CODE — QUICK REFERENCE (authoritative; do not guess syntax)

Restricted Python, parsed never executed. First statement: playbook(name=...,
description=..., when_to_use=..., inputs={JSON schema}, triggers=[trigger(...)]).
Each following statement is ONE step: <id> = combinator(...). No imports,
class, Python for/while/if, comprehensions, lambdas, 'is'. Top-level def IS
allowed (see FUNCTIONS).

COMBINATORS (the only callables, plus range() and your def functions):
  tool('tool_name', **args)                      # args may be Jinja strings
  llm(prompt, output={'field': 'str', ...}, purpose=, model=, system=)
  agent(prompt, output={...}, tools=[...])       # only when tools/memory needed
  code('''<python body>''', inputs={...})        # jailed Python, see CODE STEPS
  if_(cond, then=[...], else_=[...])
  loop(over=|while_=|until=, item_name=, body=[...], collect=,
       break_when=, max_iterations=, concurrency=)
  parallel([[...], [...]], fan_in='all')
  state(op, ...)   approve(show=[...])   halt(when=, value=)
  wait_event('event.name', filter={...}, timeout_seconds=)
  subtask('other-playbook', inputs={...}, returns={...})
Common kwargs on any step: id=, explanation=, retry=, on_error=, timeout=.
Inside body/then/else_/branches bind with walrus: (x := llm(...)).

CODE STEPS (deterministic transforms — parsing, math, formatting; needs the
plugin-inline-code-run plugin installed; no network, stdlib + pillow/pypdf/
openpyxl/segno/fpdf2):
  norm = code('''
  digits = ''.join(c for c in inputs['raw'] if c.isdigit())
  return {'phone': digits}
  ''', inputs={'raw': inputs.raw})
The body is the inside of a function: read the `inputs` dict, `return` a
JSON-serializable value. Imports ARE allowed inside the body. Read back:
steps.norm.result (your return value), steps.norm.stdout (prints, debug only).
Prefer code() over llm() for anything a regex/loop can do exactly.

FUNCTIONS (reuse a step sequence; expanded inline at compile time):
  def notify(target, note):
      msg = f'ALERT for {target}: {note}'
      sent = tool('send_message', to=target, text=msg)
  notify(inputs.owner, 'first pass')
  notify('ops', note='second pass')       # also callable inside then/body lists
def before first call; plain positional params only (no defaults/*args).
Functions are procedures — no return value; `return` is an error. Each call
expands with a unique prefix: call #1's steps are notify__msg, notify__sent
(read: steps.notify__sent, vars.notify__msg), call #2's notify_2__*. The body
may read outer steps/vars; names it defines must not collide with outer ones.

VALUE ASSIGNMENT (compute once, reuse everywhere):
  x = <expression>            # any non-combinator RHS; also (x := <expr>)
                              # inside body/then/else_
  x = inputs.count + 1
  phone = '{{ inputs.raw | regex_replace("[\\\\s\\\\-()]+") | regex_replace("^\\\\+972", "0") }}'
Read it back: bare `x` in bare expressions, `{{ vars.x }}` inside strings.
Sets a run-scoped var — persists across loop iterations.

STATE OPS (inside state(); vars persist across iterations, read as vars.<n>):
  set_('v', value)  append/extend  merge  push_back  pop_back('v', into='x')
  pop_front('v', into='x')  add_unique  incr('n')/decr('n')  delete
  Stack = push_back+pop_back; queue = push_back+pop_front; set = add_unique.
  Values are expressions: set_('frontier', '[ inputs.start_url ]').

LOOP CONFIG (exact kwargs — no other fields work):
  over= (literal list or expression) | until= (loop UNTIL true) |
  while_= (loop WHILE true — mutate a vars.* each iteration via state() and
  ALWAYS set max_iterations, or it runs to the cap) |
  break_when= (checked AFTER each iteration; stopped: 'break') |
  item_name='x' gives {{ x }} and {{ x_index }} inside the body |
  collect= evaluated after each iteration → steps.<id>.collected (without it
  only the last iteration survives) |
  concurrency=N runs bodies in parallel (default 1); bodies are ISOLATED —
  never mutate shared state in a concurrent loop, only collect merges back
  (item order); prefer concurrency=4 when the body is side-effect-free.
  Empty body=[] does nothing — nest at least one step.

EXPRESSIONS: plain strings pass verbatim — put Jinja {{ ... }} inside them.
Bare Python over inputs/vars/steps/event works (inputs.n + 1, steps.fetch.result);
f-strings work in prompts/args. Jinja FILTERS only exist inside strings.

REFERENCE SHAPES (dry_run's `references` shows the real namespace — copy paths):
  tool() output is wrapped:      steps.<id>.result.<field>
  llm()/agent() with output=:    steps.<id>.<field>
  llm()/agent() schemaless:      steps.<id>._raw   (there is NO .output)
  loop():                        steps.<id>.collected  (+ iterations, stopped)
  code():                        steps.<id>.result  (the returned value)
  value assignment / state:      vars.<name>
Dot access on step data ALWAYS reads the dict key — steps.x.result.items is
the 'items' field, never a Python method. Missing key = loud error.

JINJA FILTERS available (complete list of the useful set):
  length count first last sum min max sort unique reverse join split replace
  lower upper title trim truncate default list map select reject selectattr
  rejectattr groupby batch slice items tojson int float string abs round
  regex_replace(pattern, replacement='', count=0)
  regex_search(pattern, group=0)   regex_findall(pattern)
Tests: is defined, is none, is string, is number, is mapping, is iterable,
'in', ==/!=; selectattr('id', 'equalto', x) works.

THE LOOP: playbook_propose/playbook_edit (read → ticket → write) →
playbook_validate → playbook_dry_run (stubs effects; proves paths/branches) →
playbook_promote (specs gate it) → playbook_run → playbook_status(run_id).
A save creates a CANDIDATE; live changes only on promote.
"""

LANGUAGE_MINIREF = """\
PLAYBOOK CODE mini-reference — full spec: call playbook_language_reference.
Restricted Python, parsed never executed: no imports, no Python
for/while/if, no comprehensions; one step per statement, <id> = combinator.
Combinators: tool('name', **args) | llm(prompt, output={...}) |
  agent(prompt, output=, tools=) | code('''body''', inputs={...}) |
  if_(cond, then=[...], else_=[...]) |
  loop(over=|while_=|until=, item_name=, body=[...], collect=,
       max_iterations=, concurrency=) |
  parallel([[...], ...]) | state(op, ...) | approve(show=[...]) |
  wait_event(...) | subtask(...) | halt(when=, value=) | x = <expr>
Reference shapes: tool → steps.<id>.result.<field>; llm/agent with output=
  → steps.<id>.<field>; schemaless → steps.<id>._raw (there is NO .output);
  loop → steps.<id>.collected; code → steps.<id>.result; vars.<name>.
Rules agents forget: (1) Jinja filters exist only inside '{{ ... }}'
  strings; (2) loops need collect= or only the last iteration survives;
  (3) while_ loops need a state() mutation + max_iterations; (4) copy old=
  snippets verbatim from the code frame above.
"""
