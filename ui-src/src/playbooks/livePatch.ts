/**
 * 006.709 — pure helpers that apply a `ui.playbook.patch` event to a
 * PlaybookDef. Kept side-effect-free so the queue logic is unit-testable.
 */

import type { PlaybookDef, StepDef, TriggerDef } from './types'

export interface PlaybookPatchEvt {
  draft_id: string
  action: 'add_step' | 'add_trigger' | 'update_step' | 'remove_step' | 'replace'
  step?: StepDef & { [k: string]: unknown }
  step_id?: string
  trigger?: TriggerDef
  parent_step_id?: string
  parent_branch?: 'body' | 'then' | 'else'
  name?: string
}

/** Recursively walk nested step arrays (then/else/body/branches). */
function mapSteps(steps: StepDef[], fn: (s: StepDef) => StepDef | null): StepDef[] {
  const out: StepDef[] = []
  for (const s of steps) {
    const mapped = fn(s)
    if (mapped === null) continue // removed
    const next: StepDef = { ...mapped }
    if (next.then) next.then = mapSteps(next.then, fn)
    if (next.else) next.else = mapSteps(next.else, fn)
    if (next.body) next.body = mapSteps(next.body, fn)
    if (next.branches) next.branches = next.branches.map((b) => mapSteps(b, fn))
    out.push(next)
  }
  return out
}

function insertNested(
  steps: StepDef[],
  parentId: string,
  branch: 'body' | 'then' | 'else',
  step: StepDef,
): boolean {
  for (const s of steps) {
    if (s.id === parentId) {
      const key = branch === 'else' ? 'else' : branch
      const arr = (s[key] as StepDef[] | undefined) ?? []
      ;(s as unknown as Record<string, unknown>)[key] = [...arr, step]
      return true
    }
    for (const childKey of ['then', 'else', 'body'] as const) {
      const children = s[childKey]
      if (children && insertNested(children, parentId, branch, step)) return true
    }
    if (s.branches) {
      for (const b of s.branches) {
        if (insertNested(b, parentId, branch, step)) return true
      }
    }
  }
  return false
}

function stepExists(steps: StepDef[] | undefined, id: string): boolean {
  for (const s of steps ?? []) {
    if (s.id === id) return true
    if (stepExists(s.then, id) || stepExists(s.else, id) || stepExists(s.body, id)) return true
    if (s.branches?.some((b) => stepExists(b, id))) return true
  }
  return false
}

/**
 * Apply one patch to a definition. Returns the next definition plus the
 * canvas node id that should glow (undefined for removals/replace).
 */
export function applyPlaybookPatch(
  def: PlaybookDef,
  evt: PlaybookPatchEvt,
): { def: PlaybookDef; glowNodeId?: string } {
  const next: PlaybookDef = JSON.parse(JSON.stringify(def))

  switch (evt.action) {
    case 'add_step': {
      if (!evt.step) return { def }
      // Replayed/buffered patch for a step the fresh load already contains —
      // don't duplicate, just glow it.
      if (evt.step.id && stepExists(next.steps, evt.step.id)) {
        return { def: next, glowNodeId: `step-${evt.step.id}` }
      }
      if (evt.parent_step_id) {
        const ok = insertNested(
          next.steps ?? [],
          evt.parent_step_id,
          evt.parent_branch ?? 'body',
          evt.step,
        )
        if (!ok) next.steps = [...(next.steps ?? []), evt.step]
      } else {
        next.steps = [...(next.steps ?? []), evt.step]
      }
      return { def: next, glowNodeId: `step-${evt.step.id}` }
    }
    case 'update_step': {
      if (!evt.step_id || !evt.step) return { def }
      next.steps = mapSteps(next.steps ?? [], (s) =>
        s.id === evt.step_id ? ({ ...s, ...evt.step } as StepDef) : s,
      )
      return { def: next, glowNodeId: `step-${evt.step_id}` }
    }
    case 'remove_step': {
      if (!evt.step_id) return { def }
      next.steps = mapSteps(next.steps ?? [], (s) => (s.id === evt.step_id ? null : s))
      return { def: next }
    }
    case 'add_trigger': {
      if (!evt.trigger) return { def }
      next.triggers = [...(next.triggers ?? []), evt.trigger]
      return { def: next, glowNodeId: `trigger-${next.triggers.length - 1}` }
    }
    default:
      return { def }
  }
}

/** Does this patch belong to the playbook the editor is showing? */
export function patchMatchesEditor(
  evt: { draft_id?: string },
  draftId: string | undefined,
  name: string | undefined,
): boolean {
  if (!evt.draft_id) return false
  return evt.draft_id === draftId || evt.draft_id === name
}
