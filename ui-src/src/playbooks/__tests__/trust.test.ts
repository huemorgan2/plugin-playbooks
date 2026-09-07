import { describe, expect, it } from 'vitest'
import {
  probesLabel, intentLabel, failureWords, probesHeadline,
} from '../trust'
import type { TrustSummary } from '../types'

const trust = (over: Partial<TrustSummary> = {}): TrustSummary => ({
  probes: { total: 0, failed: 0, probed_at: null },
  manifest_present: false,
  ...over,
})

describe('probesLabel', () => {
  it('is amber "unchecked" with no probe rows (or no trust data at all)', () => {
    expect(probesLabel(undefined)).toEqual({ text: 'unchecked', tone: 'warn' })
    expect(probesLabel(trust())).toEqual({ text: 'unchecked', tone: 'warn' })
  })
  it('is green "tools ok" when nothing failed', () => {
    expect(probesLabel(trust({ probes: { total: 3, failed: 0, probed_at: null } })))
      .toEqual({ text: 'tools ok', tone: 'ok' })
  })
  it('is red with a count when tools are broken, pluralized', () => {
    expect(probesLabel(trust({ probes: { total: 3, failed: 1, probed_at: null } })).text)
      .toBe('1 broken tool')
    expect(probesLabel(trust({ probes: { total: 3, failed: 2, probed_at: null } })).text)
      .toBe('2 broken tools')
  })
})

describe('intentLabel', () => {
  it('flags a missing manifest in amber', () => {
    expect(intentLabel(trust())).toEqual({ text: 'no intent', tone: 'warn' })
    expect(intentLabel(trust({ manifest_present: true })))
      .toEqual({ text: 'intent ✓', tone: 'ok' })
  })
})

describe('failureWords', () => {
  it('translates every protocol class into plain words', () => {
    expect(failureWords('credential_dead')).toBe('credential dead')
    expect(failureWords('tool_missing')).toBe('tool not installed')
    expect(failureWords('blocked')).toBe('blocked by policy')
    expect(failureWords('rate_limited')).toBe('rate limited')
  })
  it('never leaks underscores for unknown classes', () => {
    expect(failureWords('some_new_class')).toBe('some new class')
    expect(failureWords(null)).toBe('failed')
  })
})

describe('headline (Connections tab)', () => {
  it('probes: honest about the unprobeable', () => {
    expect(probesHeadline(0, 0, 0)).toEqual({ text: 'Nothing verified yet', tone: 'warn' })
    expect(probesHeadline(3, 3, 0).text).toBe('All 3 tools answered')
    expect(probesHeadline(3, 1, 0).text).toBe('1 of 3 tools answered')
    expect(probesHeadline(3, 0, 0).tone).toBe('warn')
    expect(probesHeadline(3, 1, 2)).toEqual({ text: '2 tools broken', tone: 'bad' })
  })
})
