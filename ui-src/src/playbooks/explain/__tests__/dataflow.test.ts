/**
 * plans/010 phase 2: derived data-flow — everything comes from the StepDef.
 */
import { describe, it, expect } from 'vitest'
import type { StepDef } from '../../types'
import { stepReads, stepWrites, findStepById } from '../dataflow'

describe('stepReads', () => {
  it('collects refs from bare expression fields', () => {
    const reads = stepReads({
      id: 'c', kind: 'condition', when: 'steps.check.result.ok and vars.retry_count',
    })
    expect(reads).toContain('steps.check.result')
    expect(reads).toContain('vars.retry_count')
  })

  it('collects refs recursively from args and code_inputs', () => {
    const reads = stepReads({
      id: 't', kind: 'tool_call', tool: 'x',
      args: { nested: { list: ['{{ inputs.topic }}'] }, other: '{{ steps.a.result }}' },
    })
    expect(reads).toContain('inputs.topic')
    expect(reads).toContain('steps.a.result')
  })

  it('collects refs from state op values and dedupes', () => {
    const reads = stepReads({
      id: 's', kind: 'state',
      state: [
        { op: 'set', var: 'a', value: '{{ inputs.n }}' },
        { op: 'append', var: 'b', value: '{{ inputs.n }}' },
      ],
    })
    expect(reads).toEqual(['inputs.n'])
  })

  it('returns empty for a step with no references', () => {
    expect(stepReads({ id: 'h', kind: 'halt' })).toEqual([])
  })
})

describe('stepWrites', () => {
  it('result-producing kinds write steps.<id>.result', () => {
    for (const kind of ['tool_call', 'agent_step', 'llm_step', 'code', 'wait_for_event'] as const) {
      expect(stepWrites({ id: 'x', kind })).toEqual(['steps.x.result'])
    }
  })

  it('subtask writes its returns keys, or result without a returns map', () => {
    expect(stepWrites({
      id: 'sub', kind: 'subtask', returns: { ok: 'steps.a.result', n: 'vars.n' },
    })).toEqual(['steps.sub.ok', 'steps.sub.n'])
    expect(stepWrites({ id: 'sub', kind: 'subtask' })).toEqual(['steps.sub.result'])
  })

  it('loop writes collected only when collecting', () => {
    expect(stepWrites({ id: 'lp', kind: 'loop', collect: 'item.x' })).toEqual(['steps.lp.collected'])
    expect(stepWrites({ id: 'lp', kind: 'loop' })).toEqual([])
  })

  it('state writes vars, including into, never for delete', () => {
    expect(stepWrites({
      id: 's', kind: 'state',
      state: [
        { op: 'set', var: 'count', value: 1 },
        { op: 'pop_front', var: 'queue', into: 'current' },
        { op: 'delete', var: 'tmp' },
      ],
    })).toEqual(['vars.count', 'vars.queue', 'vars.current'])
  })

  it('condition and parallel write nothing themselves', () => {
    expect(stepWrites({ id: 'c', kind: 'condition' })).toEqual([])
    expect(stepWrites({ id: 'p', kind: 'parallel' })).toEqual([])
  })
})

describe('findStepById', () => {
  const tree: StepDef[] = [
    { id: 'top', kind: 'tool_call', tool: 't' },
    {
      id: 'cond', kind: 'condition', when: 'x',
      then: [{ id: 'inner-then', kind: 'halt' }],
      else: [{
        id: 'inner-loop', kind: 'loop', over: 'inputs.items',
        body: [{ id: 'deep', kind: 'code', source: 'return 1' }],
      }],
    },
    {
      id: 'par', kind: 'parallel',
      branches: [[{ id: 'branch-step', kind: 'tool_call', tool: 'u' }]],
    },
  ]

  it('finds top-level, branch-nested and loop-body steps', () => {
    expect(findStepById(tree, 'top')?.id).toBe('top')
    expect(findStepById(tree, 'inner-then')?.id).toBe('inner-then')
    expect(findStepById(tree, 'deep')?.id).toBe('deep')
    expect(findStepById(tree, 'branch-step')?.id).toBe('branch-step')
  })

  it('returns null for an unknown id', () => {
    expect(findStepById(tree, 'nope')).toBeNull()
  })
})
