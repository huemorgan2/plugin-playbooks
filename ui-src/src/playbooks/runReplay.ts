/**
 * 007.009.01 — turn a finished/in-flight run into an ordered timeline that
 * drives both the node-fire shimmer and the stack/queue visualization.
 *
 * The run's step rows are already ordered by started_at (one row per execution,
 * so loop bodies produce one row per iteration). Each frame = one step
 * execution; a `state` step's frame carries the ops it applied (with the engine-
 * computed post-op snapshot in `after`), so the viz can be reconstructed exactly
 * without re-implementing the engine.
 */

import type { PlaybookRunDetail, StepRunDetail, StateFrame, StepKind } from './types'

export interface TimelineFrame {
  index: number
  stepId: string
  kind: StepKind
  step: StepRunDetail
  stateFrames: StateFrame[]
}

export function buildTimeline(run: PlaybookRunDetail): TimelineFrame[] {
  return run.steps.map((step, index) => ({
    index,
    stepId: step.step_id,
    kind: step.kind,
    step,
    stateFrames:
      step.kind === 'state' && Array.isArray(step.outputs?.ops)
        ? (step.outputs!.ops as StateFrame[])
        : [],
  }))
}

export interface VizSnapshot {
  /** var name → current value (list/scalar/dict), using engine `after` snapshots */
  vars: Record<string, any>
  /** the ops applied at the CURRENT frame (drives enter/leave animations) */
  current: StateFrame[]
  /** insertion order of var names (first-seen) for stable layout */
  order: string[]
}

/**
 * Replay every state op from frame 0..idx (inclusive) to get the collection
 * state at that point. Uses each op's `after` snapshot (authoritative), so this
 * never diverges from what the engine actually did.
 */
export function snapshotAt(timeline: TimelineFrame[], idx: number): VizSnapshot {
  const vars: Record<string, any> = {}
  const order: string[] = []
  for (let i = 0; i <= idx && i < timeline.length; i++) {
    for (const f of timeline[i]!.stateFrames) {
      if (!(f.var in vars) && f.op !== 'delete') order.push(f.var)
      if (f.op === 'delete') {
        delete vars[f.var]
      } else {
        vars[f.var] = f.after
      }
    }
  }
  const current = idx >= 0 && idx < timeline.length ? timeline[idx]!.stateFrames : []
  return { vars, current, order: order.filter((n) => n in vars) }
}

export function isPop(op: StateFrame['op']): boolean {
  return op === 'pop_front' || op === 'pop_back'
}

export function isPush(op: StateFrame['op']): boolean {
  return op === 'push_back' || op === 'push_front' || op === 'append' || op === 'add_unique'
}

export type VarKind = 'stack' | 'queue' | 'set' | 'counter' | 'list' | 'value'

/** Infer how each var is USED across the whole run, for a nicer label/shape. */
export function classifyVars(timeline: TimelineFrame[]): Record<string, VarKind> {
  const ops: Record<string, Set<string>> = {}
  for (const fr of timeline) {
    for (const f of fr.stateFrames) {
      ;(ops[f.var] ||= new Set()).add(f.op)
    }
  }
  const out: Record<string, VarKind> = {}
  for (const [name, set] of Object.entries(ops)) {
    if (set.has('pop_front')) out[name] = 'queue'
    else if (set.has('pop_back')) out[name] = 'stack'
    else if (set.has('add_unique')) out[name] = 'set'
    else if (set.has('incr') || set.has('decr')) out[name] = 'counter'
    else if (set.has('push_back') || set.has('push_front') || set.has('append') || set.has('extend')) out[name] = 'list'
    else out[name] = 'value'
  }
  return out
}

export function hasState(timeline: TimelineFrame[]): boolean {
  return timeline.some((f) => f.stateFrames.length > 0)
}
