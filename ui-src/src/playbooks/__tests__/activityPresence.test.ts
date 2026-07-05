import { describe, it, expect } from 'vitest'
import {
  ACTIVITY_TTL_MS,
  applyActivity,
  runningNames,
  type ActivityEvent,
} from '../activityPresence'

function ev(event: ActivityEvent['event'], over: Partial<ActivityEvent> = {}): ActivityEvent {
  return { event, kind: 'playbook', meta: { playbook_name: 'daily-digest' }, label: 'Daily Digest', ...over }
}

describe('activityPresence', () => {
  it('marks running while beats are within the TTL', () => {
    const beats = new Map<string, number>()
    applyActivity(beats, ev('activity.started'), 1000)
    expect([...runningNames(beats, 1000)]).toEqual(['daily-digest'])
    // a heartbeat 5s later is still within the 8s TTL
    applyActivity(beats, ev('activity.heartbeat'), 6000)
    expect([...runningNames(beats, 6000)]).toEqual(['daily-digest'])
  })

  it('clears after the TTL elapses with no further beat', () => {
    const beats = new Map<string, number>()
    applyActivity(beats, ev('activity.started'), 1000)
    const later = 1000 + ACTIVITY_TTL_MS + 1
    expect(runningNames(beats, later).size).toBe(0)
    // and the stale entry is pruned from the map
    expect(beats.has('daily-digest')).toBe(false)
  })

  it('clears instantly on activity.completed', () => {
    const beats = new Map<string, number>()
    applyActivity(beats, ev('activity.started'), 1000)
    applyActivity(beats, ev('activity.completed'), 1500)
    expect(runningNames(beats, 1500).size).toBe(0)
  })

  it('keys by meta.playbook_name, falling back to label', () => {
    const beats = new Map<string, number>()
    applyActivity(beats, { event: 'activity.started', kind: 'playbook', label: 'Only Label' }, 1000)
    expect([...runningNames(beats, 1000)]).toEqual(['Only Label'])
  })

  it('ignores non-playbook activities', () => {
    const beats = new Map<string, number>()
    applyActivity(beats, { event: 'activity.started', kind: 'import', label: 'X' }, 1000)
    expect(runningNames(beats, 1000).size).toBe(0)
  })

  it('tracks multiple concurrent runs independently', () => {
    const beats = new Map<string, number>()
    applyActivity(beats, ev('activity.started', { meta: { playbook_name: 'a' } }), 1000)
    applyActivity(beats, ev('activity.started', { meta: { playbook_name: 'b' } }), 1000)
    expect(runningNames(beats, 1000).size).toBe(2)
    applyActivity(beats, ev('activity.completed', { meta: { playbook_name: 'a' } }), 1200)
    expect([...runningNames(beats, 1200)]).toEqual(['b'])
  })
})
