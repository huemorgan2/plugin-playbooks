/**
 * plans/010 phase 2: derived data flow for one step — what it reads (template
 * references anywhere in its definition) and what it writes (outputs, state
 * vars). Pure derivation from the StepDef; nothing here is authored.
 */

import type { StepDef } from '../types'
import { extractRefs } from './tokens'

/** Recursively pull template refs out of any definition value. */
function refsIn(value: unknown, out: Set<string>) {
  if (typeof value === 'string') {
    for (const r of extractRefs(value)) out.add(r)
  } else if (Array.isArray(value)) {
    for (const v of value) refsIn(v, out)
  } else if (value && typeof value === 'object') {
    for (const v of Object.values(value)) refsIn(v, out)
  }
}

/** Bare expressions (no {{ }} needed) vs templated strings — both just tokenize. */
const EXPR_FIELDS: (keyof StepDef)[] = [
  'when', 'over', 'while', 'until', 'break_when', 'collect',
]
const VALUE_FIELDS: (keyof StepDef)[] = [
  'args', 'code_inputs', 'prompt', 'system', 'event_filter',
  'inputs_map', 'value', 'show',
]

export function stepReads(step: StepDef): string[] {
  const out = new Set<string>()
  for (const f of EXPR_FIELDS) refsIn(step[f], out)
  for (const f of VALUE_FIELDS) refsIn(step[f], out)
  for (const op of step.state || []) refsIn(op.value, out)
  return [...out]
}

/** Find a step anywhere in the tree (branches, loop bodies) by its id. */
export function findStepById(steps: StepDef[], id: string): StepDef | null {
  for (const s of steps) {
    if (s.id === id) return s
    for (const nested of [s.then, s.else, s.body, ...(s.branches || [])]) {
      if (nested) {
        const hit = findStepById(nested, id)
        if (hit) return hit
      }
    }
  }
  return null
}

export function stepWrites(step: StepDef): string[] {
  const out: string[] = []
  switch (step.kind) {
    case 'tool_call':
    case 'agent_step':
    case 'llm_step':
    case 'code':
      out.push(`steps.${step.id}.result`)
      break
    case 'subtask':
      for (const key of Object.keys(step.returns || {})) out.push(`steps.${step.id}.${key}`)
      if (!step.returns) out.push(`steps.${step.id}.result`)
      break
    case 'loop':
      if (step.collect) out.push(`steps.${step.id}.collected`)
      break
    case 'state':
      for (const op of step.state || []) {
        const w = op.op === 'delete' ? null : `vars.${op.var.replace(/^vars\./, '')}`
        if (w) out.push(w)
        if (op.into) out.push(`vars.${op.into.replace(/^vars\./, '')}`)
      }
      break
    case 'wait_for_event':
      out.push(`steps.${step.id}.result`)
      break
    default:
      break
  }
  return [...new Set(out)]
}
