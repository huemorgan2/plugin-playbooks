// live-events fan-out: one SSE connection shared by activity + playbook
// subscribers, frames routed by prefix, connection torn down with the last
// unsubscribe.
import { describe, it, expect, vi, beforeEach } from 'vitest'

type Msg = { event: string; data: string }
let onMessage: ((msg: Msg) => void) | null = null
let openCount = 0

vi.mock('@microsoft/fetch-event-source', () => ({
  fetchEventSource: vi.fn((_url: string, opts: { signal: AbortSignal; onmessage: (m: Msg) => void }) => {
    openCount += 1
    onMessage = opts.onmessage
    return new Promise<void>((resolve) => {
      opts.signal.addEventListener('abort', () => resolve())
    })
  }),
}))
vi.mock('../../lib/auth', () => ({
  getTokenAsync: async () => 'tok',
  invalidateToken: () => {},
}))

import { subscribeActivityEvents, subscribePlaybookEvents } from '../../lib/events'

const flush = () => new Promise((r) => setTimeout(r, 0))

beforeEach(() => {
  onMessage = null
  openCount = 0
})

describe('events fan-out', () => {
  it('routes activity.* and playbook.* to their subscribers over ONE connection', async () => {
    const acts: string[] = []
    const pbs: string[] = []
    const un1 = subscribeActivityEvents((i) => acts.push(i.event))
    const un2 = subscribePlaybookEvents((e) => pbs.push(`${e.event}:${e.run_id}`))
    await flush()
    expect(openCount).toBe(1)

    onMessage!({ event: 'activity.heartbeat', data: '{"kind":"playbook"}' })
    onMessage!({ event: 'playbook.step.started', data: '{"run_id":"r1","step_id":"s1"}' })
    onMessage!({ event: 'heartbeat', data: '{}' }) // keepalive — ignored

    expect(acts).toEqual(['activity.heartbeat'])
    expect(pbs).toEqual(['playbook.step.started:r1'])
    un1(); un2()
  })

  it('closes the stream when the last subscriber leaves, reopens on the next', async () => {
    const un = subscribePlaybookEvents(() => {})
    await flush()
    expect(openCount).toBe(1)
    un()
    const un2 = subscribeActivityEvents(() => {})
    await flush()
    expect(openCount).toBe(2) // fresh connection after full teardown
    un2()
  })

  it('a throwing handler does not stall the fan-out', async () => {
    const seen: string[] = []
    const unA = subscribePlaybookEvents(() => { throw new Error('boom') })
    const unB = subscribePlaybookEvents((e) => seen.push(e.event))
    await flush()
    onMessage!({ event: 'playbook.run.started', data: '{"run_id":"r2","playbook_name":"p"}' })
    expect(seen).toEqual(['playbook.run.started'])
    unA(); unB()
  })
})
