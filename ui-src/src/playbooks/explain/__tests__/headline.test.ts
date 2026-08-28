import { describe, it, expect } from 'vitest'
import { headline } from '../headline'
import type { StepDef } from '../../types'

const s = (partial: Partial<StepDef> & { kind: StepDef['kind'] }): StepDef =>
  ({ id: 'x', ...partial }) as StepDef

describe('headline', () => {
  it('is built from definition fields, kind by kind', () => {
    expect(headline(s({ kind: 'tool_call', tool: 'web_search' }))).toBe('Calls web_search')
    expect(headline(s({ kind: 'condition', when: 'a > 1' }))).toBe('Branches on a > 1')
    expect(headline(s({ kind: 'loop', over: 'steps.list.result', item_name: 'url' })))
      .toBe('For each url in steps.list.result')
    expect(headline(s({ kind: 'loop', while: 'vars.queue' }))).toBe('Repeats while vars.queue')
    expect(headline(s({ kind: 'subtask', playbook: 'crawl-site' }))).toBe('Runs playbook crawl-site')
    expect(headline(s({ kind: 'wait_for_event', event: 'email.received' }))).toBe('Waits for email.received')
    expect(headline(s({ kind: 'parallel', branches: [[], []] as any }))).toBe('Runs 2 branches at once')
    expect(headline(s({ kind: 'state', state: [{ op: 'incr', var: 'vars.count' }] }))).toBe('incr vars.count')
    expect(headline(s({ kind: 'halt', when: 'vars.done' }))).toBe('Stops the run if vars.done')
    expect(headline(s({ kind: 'code', source: 'x = 1\nreturn x' }))).toBe('Runs Python (2 lines)')
  })

  it('never uses step.explanation', () => {
    const step = s({ kind: 'tool_call', tool: 'web_search', explanation: 'PROSE THAT COULD DRIFT' })
    expect(headline(step)).not.toContain('PROSE')
  })

  it('truncates long expressions', () => {
    const long = 'x'.repeat(200)
    expect(headline(s({ kind: 'condition', when: long })).length).toBeLessThanOrEqual(64)
  })
})
