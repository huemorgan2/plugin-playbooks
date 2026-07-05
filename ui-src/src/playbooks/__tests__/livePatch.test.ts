import { describe, expect, it } from 'vitest'
import { applyPlaybookPatch, patchMatchesEditor } from '../livePatch'
import type { PlaybookDef, StepDef } from '../types'

function def(steps: StepDef[] = []): PlaybookDef {
  return {
    name: 'pb',
    display_name: 'PB',
    description: '',
    when_to_use: '',
    agent_autonomy: 'agent_must_confirm',
    triggers: [],
    steps,
  }
}

describe('applyPlaybookPatch', () => {
  it('add_step appends at top level and glows the new node', () => {
    const { def: next, glowNodeId } = applyPlaybookPatch(def(), {
      draft_id: 'x',
      action: 'add_step',
      step: { id: 's1', kind: 'tool_call', tool: 'list_credentials' },
    })
    expect(next.steps.map((s) => s.id)).toEqual(['s1'])
    expect(glowNodeId).toBe('step-s1')
  })

  it('add_step nests into a loop body via parent_step_id', () => {
    const base = def([{ id: 'loop1', kind: 'loop', body: [] }])
    const { def: next } = applyPlaybookPatch(base, {
      draft_id: 'x',
      action: 'add_step',
      step: { id: 'inner', kind: 'agent_step', prompt: 'p' },
      parent_step_id: 'loop1',
      parent_branch: 'body',
    })
    expect(next.steps[0]!.body!.map((s) => s.id)).toEqual(['inner'])
  })

  it('add_step nests into a condition else branch', () => {
    const base = def([{ id: 'c1', kind: 'condition', when: 'x', then: [], else: [] }])
    const { def: next } = applyPlaybookPatch(base, {
      draft_id: 'x',
      action: 'add_step',
      step: { id: 'e1', kind: 'tool_call', tool: 't' },
      parent_step_id: 'c1',
      parent_branch: 'else',
    })
    expect(next.steps[0]!.else!.map((s) => s.id)).toEqual(['e1'])
  })

  it('update_step merges config and glows, even when nested', () => {
    const base = def([
      { id: 'loop1', kind: 'loop', body: [{ id: 'inner', kind: 'agent_step', prompt: 'old' }] },
    ])
    const { def: next, glowNodeId } = applyPlaybookPatch(base, {
      draft_id: 'x',
      action: 'update_step',
      step_id: 'inner',
      step: { id: 'inner', kind: 'agent_step', prompt: 'new' },
    })
    expect(next.steps[0]!.body![0]!.prompt).toBe('new')
    expect(glowNodeId).toBe('step-inner')
  })

  it('remove_step deletes nested steps and does not glow', () => {
    const base = def([
      { id: 'a', kind: 'tool_call', tool: 't' },
      { id: 'loop1', kind: 'loop', body: [{ id: 'inner', kind: 'agent_step' }] },
    ])
    const { def: next, glowNodeId } = applyPlaybookPatch(base, {
      draft_id: 'x',
      action: 'remove_step',
      step_id: 'inner',
    })
    expect(next.steps[1]!.body).toEqual([])
    expect(glowNodeId).toBeUndefined()
  })

  it('add_trigger appends and glows trigger node id', () => {
    const { def: next, glowNodeId } = applyPlaybookPatch(def(), {
      draft_id: 'x',
      action: 'add_trigger',
      trigger: { event: 'message.received' },
    })
    expect(next.triggers).toHaveLength(1)
    expect(glowNodeId).toBe('trigger-0')
  })

  it('does not mutate the input definition', () => {
    const base = def([{ id: 'a', kind: 'tool_call', tool: 't' }])
    applyPlaybookPatch(base, {
      draft_id: 'x',
      action: 'add_step',
      step: { id: 'b', kind: 'tool_call', tool: 't2' },
    })
    expect(base.steps).toHaveLength(1)
  })
})

describe('patchMatchesEditor', () => {
  it('matches by draft uuid or playbook name', () => {
    expect(patchMatchesEditor({ draft_id: 'uuid-1' }, 'uuid-1', undefined)).toBe(true)
    expect(patchMatchesEditor({ draft_id: 'morning-digest' }, undefined, 'morning-digest')).toBe(true)
    expect(patchMatchesEditor({ draft_id: 'other' }, 'uuid-1', 'morning-digest')).toBe(false)
    expect(patchMatchesEditor({}, 'uuid-1', 'x')).toBe(false)
  })
})
