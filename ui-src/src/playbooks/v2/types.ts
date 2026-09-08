// plans/032 phase 10: the python playbook graph as `GET /playbooks/{name}/graph`
// returns it (plugin_playbooks/v2/graph.py) — a block tree keyed on the
// checker's call-site ids. Node ids never derive from line numbers.
import type { NodeLook } from '../types'

export type V2EffectKind =
  | 'tool' | 'llm' | 'agent' | 'subtask' | 'approve' | 'wait_event'
  | 'now' | 'random' | 'log'

export type V2NodeKind =
  | V2EffectKind
  | 'gather' | 'if' | 'for' | 'while' | 'compute' | 'error_boundary'

interface V2ItemBase {
  node: string
  kind: V2NodeKind
  label: string
  line: number
  end_line: number
}

/** One `ctx.*` call site: `node` = `step-<call_site_id>`. */
export interface V2StepItem extends V2ItemBase {
  kind: V2EffectKind
  call_site_id: string
  col: number
  sublabel: string | null
  loop_depth: number
  in_try: boolean
}

/** `ctx.gather(...)`: the fan-out over its argument call sites. */
export interface V2GatherItem extends V2ItemBase {
  kind: 'gather'
  call_site_id: null
  col: number
  sublabel: string | null
  args: V2StepItem[]
}

/** Code between effects, collapsed. */
export interface V2ComputeItem extends V2ItemBase {
  kind: 'compute'
  lines: number
}

export interface V2IfItem extends V2ItemBase {
  kind: 'if'
  then: V2Block
  else: V2Block | null
}

export interface V2LoopItem extends V2ItemBase {
  kind: 'for' | 'while'
  body: V2Block
}

export interface V2Handler {
  label: string
  line: number
  end_line: number
  body: V2Block
}

export interface V2TryItem extends V2ItemBase {
  kind: 'error_boundary'
  body: V2Block
  handlers: V2Handler[]
  finally: V2Block | null
}

export type V2Item =
  | V2StepItem | V2GatherItem | V2ComputeItem | V2IfItem | V2LoopItem | V2TryItem

export interface V2Block {
  id: string
  items: V2Item[]
}

export interface V2Graph {
  name: string
  version: number | null
  format: 'python'
  triggers: { event?: string; cron?: string; [k: string]: unknown }[]
  /** Pre-order: `trigger-<n>` first, then every item (args and child blocks after their parent). */
  node_ids: string[]
  root: V2Block
}

/** The pblang look each python node wears on the canvas (colour + icon). */
export const V2_LOOK: Record<V2NodeKind, NodeLook> = {
  tool: 'tool_call',
  llm: 'llm_step',
  agent: 'agent_step',
  subtask: 'subtask',
  approve: 'wait_for_approval',
  wait_event: 'wait_for_event',
  now: 'compute',
  random: 'compute',
  log: 'compute',
  gather: 'parallel',
  if: 'condition',
  for: 'loop',
  while: 'loop',
  compute: 'compute',
  error_boundary: 'error_boundary',
}

export function lookOf(kind: string): NodeLook {
  return (V2_LOOK as Record<string, NodeLook>)[kind] ?? 'tool_call'
}
