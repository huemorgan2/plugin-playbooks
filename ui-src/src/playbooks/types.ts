export type StepKind =
  | 'tool_call'
  | 'agent_step'
  | 'llm_step'
  | 'condition'
  | 'parallel'
  | 'wait_for_approval'
  | 'wait_for_event'
  | 'subtask'
  | 'loop'
  | 'state'
  | 'halt'
  | 'code'

export type RunStatus =
  | 'pending' | 'running' | 'completed' | 'done' | 'failed' | 'waiting' | 'cancelled'

// plans/032 phase 10: the looks a canvas node can wear — every pblang kind
// plus the two python-only shapes (code between effects, `try` boundary).
// StepKind itself stays the pblang step vocabulary.
export type NodeLook = StepKind | 'compute' | 'error_boundary'

// 007.009.01: one op a `state` step applies to a run-scoped variable.
export interface StateOp {
  op:
    | 'set' | 'append' | 'extend'
    | 'push_back' | 'push_front' | 'pop_back' | 'pop_front'
    | 'add_unique' | 'incr' | 'decr' | 'merge' | 'delete'
  var: string
  value?: any
  into?: string
}

export interface StepDef {
  id: string
  kind: StepKind
  explanation?: string
  tool?: string
  args?: Record<string, any>
  prompt?: string
  system?: string
  purpose?: string
  model?: string
  output_schema?: Record<string, any>
  tools?: string[]
  when?: string
  then?: StepDef[]
  else?: StepDef[]
  branches?: StepDef[][]
  fan_in?: string
  event?: string
  event_filter?: Record<string, any>
  playbook?: string
  inputs_map?: Record<string, string>
  returns?: Record<string, string>
  over?: string | any[]
  body?: StepDef[]
  until?: string
  while?: string
  break_when?: string
  concurrency?: number
  item_name?: string
  collect?: string
  max_iterations?: number
  // code (plans/004): jailed python; return value → steps.<id>.result
  source?: string
  code_inputs?: Record<string, any>
  // state / halt
  state?: StateOp[]
  value?: any
  show?: string[]
  timeout_seconds?: number
  timeout?: number
  retry?: { max: number; backoff_seconds: number }
  on_error?: string
}

export interface TriggerDef {
  event?: string
  cron?: string
  filter?: Record<string, any>
  map?: Record<string, string>
  if?: string
}

export interface PlaybookDef {
  name: string
  display_name: string
  description: string
  explanation?: string
  when_to_use: string
  agent_autonomy: string
  triggers: TriggerDef[]
  steps: StepDef[]
}

// 0.13.0 (plans/002 phase 6): per-playbook trust data for the list badges.
export interface TrustSummary {
  probes: { total: number; failed: number; probed_at: string | null }
  manifest_present: boolean
}

export type ProbeStatus = 'ok' | 'unprobeable' | 'failed'

export interface ProbeEntry {
  tool: string
  status: ProbeStatus
  failure_class: string | null
  detail: string | null
  probed_at: string | null
}

export interface PlaybookSummary {
  id: string
  name: string
  display_name: string
  description: string | null
  status: string
  agent_autonomy: string
  // plans/016 phase 6: owner-switchable publish gate (Settings → Publish).
  publish_require_run?: boolean
  version: number
  live_version?: number
  candidate_version?: number | null
  // plans/032 phase 04/10: the UI picks its version view by it (python → V2Canvas).
  format?: 'pblang' | 'python'
  trust?: TrustSummary
  // plans/001: run history, computed server-side over the last 30 days.
  last_run_at?: string | null
  runs_per_day?: number
  runs_window?: number
}

// plans/016 phase 2: one version's full content (`GET /versions/{n}`).
export interface VersionDetail {
  version: number
  definition: PlaybookDef
  code: string | null
  manifest: string | null
  author: string
  message: string
  created_at: string
  promoted_from: number | null
  live: boolean
  candidate: boolean
  runs: number
  // plans/032 phase 10: the language of this version's code.
  format?: 'pblang' | 'python'
}

export interface PlaybookRunSummary {
  id: string
  status: RunStatus
  trigger: string
  playbook_version?: number
  started_at: string | null
  completed_at: string | null
}

// 007.009.01: a single state op as recorded during a run (for the viz panel).
export interface StateFrame {
  op: StateOp['op']
  var: string
  item?: any
  added?: any
  into?: string
  after?: any
  step_id?: string
}

export interface StepRunDetail {
  step_id: string
  // plans/032 phase 04: a python run's rows are effects (`tool`, `log`, …),
  // not pblang step kinds — the string branch keeps StepKind's completions.
  kind: StepKind | (string & {})
  status: RunStatus
  inputs: Record<string, any> | null
  outputs: Record<string, any> | null
  error: string | null
  retry_count: number | null
  cost_cents: number | null
  started_at: string | null
  completed_at: string | null
}

// plans/032 phase 10: one journal row projected onto the graph
// (`node` = `step-<call_site_id>`, `occurrence` 1-based; docs/v2.md §6).
export interface TraceRow {
  seq: number
  node: string
  call_site_id: string
  occurrence: number
  kind: string
  journal_status: string
  status: RunStatus
  error: { type: string; message: string } | null
  dry: boolean
  started_at: string | null
  ended_at: string | null
  ms: number | null
  args?: any
  result?: any
  parked_on?: Record<string, any>
}

export interface PlaybookRunDetail extends PlaybookRunSummary {
  inputs: Record<string, any>
  steps: StepRunDetail[]
  // plans/032 phase 10: present only for journaled (python) runs.
  trace?: TraceRow[]
  failed_line?: number | null
  error?: string | null
  error_type?: string | null
  traceback?: string | null
}

// 006.709: `glow` is the RGB triplet of the kind's 400-level color — the
// node-arrive animation reads it via the --glow-rgb CSS variable so each
// node glows in its own color family.
export const STEP_COLORS: Record<NodeLook, { bg: string; border: string; text: string; glow: string }> = {
  agent_step:        { bg: 'bg-indigo-950/60',  border: 'border-indigo-500/40', text: 'text-indigo-200', glow: '129 140 248' },
  llm_step:          { bg: 'bg-fuchsia-950/60', border: 'border-fuchsia-500/40', text: 'text-fuchsia-200', glow: '232 121 249' },
  tool_call:         { bg: 'bg-teal-950/60',    border: 'border-teal-500/40',   text: 'text-teal-200',   glow: '45 212 191' },
  condition:         { bg: 'bg-amber-950/60',   border: 'border-amber-500/40',  text: 'text-amber-200',  glow: '251 191 36' },
  parallel:          { bg: 'bg-sky-950/60',     border: 'border-sky-500/40',    text: 'text-sky-200',    glow: '56 189 248' },
  wait_for_approval: { bg: 'bg-orange-950/60',  border: 'border-orange-500/40', text: 'text-orange-200', glow: '251 146 60' },
  wait_for_event:    { bg: 'bg-orange-950/60',  border: 'border-orange-500/40', text: 'text-orange-200', glow: '251 146 60' },
  subtask:           { bg: 'bg-violet-950/60',  border: 'border-violet-500/40', text: 'text-violet-200', glow: '167 139 250' },
  loop:              { bg: 'bg-purple-950/60',  border: 'border-purple-500/40', text: 'text-purple-200', glow: '192 132 252' },
  state:             { bg: 'bg-emerald-950/60', border: 'border-emerald-500/40', text: 'text-emerald-200', glow: '52 211 153' },
  halt:              { bg: 'bg-rose-950/60',    border: 'border-rose-500/40',   text: 'text-rose-200',   glow: '251 113 133' },
  code:              { bg: 'bg-cyan-950/60',    border: 'border-cyan-500/40',   text: 'text-cyan-200',   glow: '34 211 238' },
  // plans/032 phase 10: python-only looks
  compute:           { bg: 'bg-slate-950/60',   border: 'border-slate-500/40',  text: 'text-slate-200',  glow: '148 163 184' },
  error_boundary:    { bg: 'bg-rose-950/40',    border: 'border-rose-500/40',   text: 'text-rose-200',   glow: '251 113 133' },
}

export const STATUS_COLORS: Record<RunStatus, string> = {
  pending:   'text-ink-400',
  running:   'text-blue-400 animate-pulse',
  completed: 'text-emerald-400',
  done:      'text-emerald-400',
  failed:    'text-rose-400',
  waiting:   'text-amber-400',
  cancelled: 'text-ink-500',
}
