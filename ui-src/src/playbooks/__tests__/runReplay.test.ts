import { describe, expect, it } from 'vitest'
import {
  buildTimeline, snapshotAt, classifyVars, hasState, isPop, isPush,
} from '../runReplay'
import type { PlaybookRunDetail, StepRunDetail } from '../types'

function step(p: Partial<StepRunDetail>): StepRunDetail {
  return {
    step_id: 's',
    kind: 'tool_call',
    status: 'completed',
    inputs: null,
    outputs: null,
    error: null,
    retry_count: null,
    cost_cents: null,
    started_at: null,
    completed_at: null,
    ...p,
  }
}

function run(steps: StepRunDetail[]): PlaybookRunDetail {
  return {
    id: 'r1',
    status: 'completed',
    trigger: 'manual',
    started_at: null,
    completed_at: null,
    inputs: {},
    steps,
  }
}

describe('buildTimeline', () => {
  it('keeps run-step order (one frame per execution)', () => {
    const tl = buildTimeline(run([
      step({ step_id: 'a', kind: 'tool_call' }),
      step({ step_id: 'b', kind: 'state', outputs: { ops: [{ op: 'set', var: 'x', after: [] }] } }),
      step({ step_id: 'c', kind: 'llm_step' }),
    ]))
    expect(tl.map((f) => f.stepId)).toEqual(['a', 'b', 'c'])
    expect(tl[0]!.index).toBe(0)
  })

  it('extracts state frames only from state steps', () => {
    const tl = buildTimeline(run([
      step({ step_id: 'a', kind: 'tool_call', outputs: { result: 'x' } }),
      step({ step_id: 'b', kind: 'state', outputs: { ops: [{ op: 'push_back', var: 'q', item: 1, after: [1] }] } }),
    ]))
    expect(tl[0]!.stateFrames).toEqual([])
    expect(tl[1]!.stateFrames).toHaveLength(1)
    expect(tl[1]!.stateFrames[0]!.var).toBe('q')
  })
})

describe('snapshotAt — queue (FIFO) vs stack (LIFO)', () => {
  // A queue: push_back 3 items, then pop_front twice.
  const queueRun = run([
    step({ step_id: 'p', kind: 'state', outputs: { ops: [{ op: 'push_back', var: 'q', item: 'a', after: ['a'] }] } }),
    step({ step_id: 'p', kind: 'state', outputs: { ops: [{ op: 'push_back', var: 'q', item: 'b', after: ['a', 'b'] }] } }),
    step({ step_id: 'p', kind: 'state', outputs: { ops: [{ op: 'push_back', var: 'q', item: 'c', after: ['a', 'b', 'c'] }] } }),
    step({ step_id: 'd', kind: 'state', outputs: { ops: [{ op: 'pop_front', var: 'q', item: 'a', after: ['b', 'c'] }] } }),
    step({ step_id: 'd', kind: 'state', outputs: { ops: [{ op: 'pop_front', var: 'q', item: 'b', after: ['c'] }] } }),
  ])

  it('reconstructs the var at each cursor from engine `after` snapshots', () => {
    const tl = buildTimeline(queueRun)
    expect(snapshotAt(tl, 0).vars.q).toEqual(['a'])
    expect(snapshotAt(tl, 2).vars.q).toEqual(['a', 'b', 'c'])
    // FIFO: first popped is 'a', then 'b' → 'c' remains.
    expect(snapshotAt(tl, 3).vars.q).toEqual(['b', 'c'])
    expect(snapshotAt(tl, 4).vars.q).toEqual(['c'])
  })

  it('exposes the op applied at the cursor (what just left)', () => {
    const tl = buildTimeline(queueRun)
    const snap = snapshotAt(tl, 3)
    expect(snap.current[0]!.op).toBe('pop_front')
    expect(snap.current[0]!.item).toBe('a')
  })

  it('stack pops the last pushed (LIFO)', () => {
    const stackRun = run([
      step({ step_id: 'p', kind: 'state', outputs: { ops: [{ op: 'push_back', var: 's', item: 1, after: [1] }] } }),
      step({ step_id: 'p', kind: 'state', outputs: { ops: [{ op: 'push_back', var: 's', item: 2, after: [1, 2] }] } }),
      step({ step_id: 'd', kind: 'state', outputs: { ops: [{ op: 'pop_back', var: 's', item: 2, after: [1] }] } }),
    ])
    const tl = buildTimeline(stackRun)
    expect(snapshotAt(tl, 2).vars.s).toEqual([1])
    expect(snapshotAt(tl, 2).current[0]!.item).toBe(2)
  })

  it('delete removes the var from the snapshot', () => {
    const tl = buildTimeline(run([
      step({ step_id: 'a', kind: 'state', outputs: { ops: [{ op: 'set', var: 'x', after: [1] }] } }),
      step({ step_id: 'b', kind: 'state', outputs: { ops: [{ op: 'delete', var: 'x', after: null }] } }),
    ]))
    expect(snapshotAt(tl, 0).vars.x).toEqual([1])
    expect('x' in snapshotAt(tl, 1).vars).toBe(false)
  })
})

describe('classifyVars (type inference)', () => {
  it('infers queue / stack / set / counter / list from ops used', () => {
    const tl = buildTimeline(run([
      step({ step_id: '1', kind: 'state', outputs: { ops: [{ op: 'push_back', var: 'q', after: [] }] } }),
      step({ step_id: '2', kind: 'state', outputs: { ops: [{ op: 'pop_front', var: 'q', after: [] }] } }),
      step({ step_id: '3', kind: 'state', outputs: { ops: [{ op: 'pop_back', var: 'stk', after: [] }] } }),
      step({ step_id: '4', kind: 'state', outputs: { ops: [{ op: 'add_unique', var: 'seen', after: [] }] } }),
      step({ step_id: '5', kind: 'state', outputs: { ops: [{ op: 'incr', var: 'n', after: 1 }] } }),
      step({ step_id: '6', kind: 'state', outputs: { ops: [{ op: 'append', var: 'acc', after: [] }] } }),
    ]))
    const kinds = classifyVars(tl)
    expect(kinds.q).toBe('queue')
    expect(kinds.stk).toBe('stack')
    expect(kinds.seen).toBe('set')
    expect(kinds.n).toBe('counter')
    expect(kinds.acc).toBe('list')
  })
})

describe('helpers', () => {
  it('hasState is true only when a frame carries state ops', () => {
    expect(hasState(buildTimeline(run([step({ kind: 'tool_call' })])))).toBe(false)
    expect(hasState(buildTimeline(run([
      step({ kind: 'state', outputs: { ops: [{ op: 'set', var: 'x', after: [] }] } }),
    ])))).toBe(true)
  })

  it('isPop / isPush classify ops', () => {
    expect(isPop('pop_front')).toBe(true)
    expect(isPop('pop_back')).toBe(true)
    expect(isPop('push_back')).toBe(false)
    expect(isPush('push_back')).toBe(true)
    expect(isPush('add_unique')).toBe(true)
    expect(isPush('pop_front')).toBe(false)
  })
})
