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

export type RunStatus =
  | 'pending' | 'running' | 'completed' | 'done' | 'failed' | 'waiting' | 'cancelled'

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
  over?: string
  body?: StepDef[]
  until?: string
  while?: string
  break_when?: string
  concurrency?: number
  count?: number
  item_name?: string
  collect?: string
  max_iterations?: number
  // state / halt
  state?: StateOp[]
  value?: any
  show?: string[]
  timeout_seconds?: number
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

export interface PlaybookSummary {
  id: string
  name: string
  display_name: string
  description: string | null
  status: string
  agent_autonomy: string
  version: number
  // plans/001: run history, computed server-side over the last 30 days.
  last_run_at?: string | null
  runs_per_day?: number
  runs_window?: number
}

export interface PlaybookRunSummary {
  id: string
  status: RunStatus
  trigger: string
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
  kind: StepKind
  status: RunStatus
  inputs: Record<string, any> | null
  outputs: Record<string, any> | null
  error: string | null
  retry_count: number | null
  cost_cents: number | null
  started_at: string | null
  completed_at: string | null
}

export interface PlaybookRunDetail extends PlaybookRunSummary {
  inputs: Record<string, any>
  steps: StepRunDetail[]
}

// 006.709: `glow` is the RGB triplet of the kind's 400-level color — the
// node-arrive animation reads it via the --glow-rgb CSS variable so each
// node glows in its own color family.
export const STEP_COLORS: Record<StepKind, { bg: string; border: string; text: string; glow: string }> = {
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
