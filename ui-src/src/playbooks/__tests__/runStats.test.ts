import { describe, expect, it } from 'vitest'
import { lastRunLabel, rateLabel } from '../runStats'

const NOW = new Date('2026-07-23T12:00:00Z').getTime()
const ago = (ms: number) => new Date(NOW - ms).toISOString()

describe('lastRunLabel', () => {
  it('says how long ago, in the biggest unit that still reads as a number', () => {
    expect(lastRunLabel(ago(30_000), NOW)).toBe('ran just now')
    expect(lastRunLabel(ago(9 * 60_000), NOW)).toBe('ran 9m ago')
    expect(lastRunLabel(ago(2 * 3600_000), NOW)).toBe('ran 2h ago')
    expect(lastRunLabel(ago(3 * 86400_000), NOW)).toBe('ran 3d ago')
    expect(lastRunLabel(ago(90 * 86400_000), NOW)).toBe('ran 3mo ago')
  })

  it('never leaves the slot blank', () => {
    expect(lastRunLabel(null, NOW)).toBe('never run')
    expect(lastRunLabel('not a date', NOW)).toBe('never run')
  })
})

describe('rateLabel', () => {
  it('shows one decimal per day', () => {
    expect(rateLabel({ runs_per_day: 3.4, runs_window: 102 })).toBe('3.4/day')
  })

  it('floors at <0.1 so a rare runner never reads as never', () => {
    expect(rateLabel({ runs_per_day: 0.03, runs_window: 1 })).toBe('<0.1/day')
  })

  it('says nothing when the playbook never ran — the last-run label covers it', () => {
    expect(rateLabel({ runs_per_day: 0, runs_window: 0 })).toBe('')
    expect(rateLabel({})).toBe('')
  })
})
