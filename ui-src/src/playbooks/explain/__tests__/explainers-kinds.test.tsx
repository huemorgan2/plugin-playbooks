// @vitest-environment jsdom
/**
 * plans/010 phase 2: zero-data render tests for the ten explainers added in
 * this phase, plus DataFlow and FooterChips. Every data token on screen must
 * come from the fixture; absent fields leave no trace.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, cleanup, fireEvent } from '@testing-library/react'
import type { StepDef } from '../../types'
import { STEP_EXPLAINERS, DataFlow, FooterChips } from '../registry'

afterEach(cleanup)

function renderKind(step: StepDef, onSelectStep?: (s: StepDef) => void) {
  const Explainer = STEP_EXPLAINERS[step.kind]!
  expect(Explainer).toBeTruthy()
  return render(<Explainer step={step} onSelectStep={onSelectStep} />)
}

describe('agent_step explainer', () => {
  it('shows prompt refs, tool chips and output schema fields', () => {
    const { container } = renderKind({
      id: 'a1', kind: 'agent_step',
      prompt: 'Summarize {{ inputs.doc }}',
      tools: ['web_search', 'files_read'],
      output_schema: { type: 'object', properties: { summary: { type: 'string' } } },
    })
    const text = container.textContent!
    expect(text).toContain('Summarize')
    expect(text).toContain('inputs.doc')
    expect(text).toContain('web_search')
    expect(text).toContain('files_read')
    expect(text).toContain('summary')
  })

  it('renders no tools or schema sections when absent', () => {
    const { container } = renderKind({ id: 'a1', kind: 'agent_step', prompt: 'go' })
    expect(container.textContent).not.toContain('Tools')
    expect(container.textContent).not.toContain('Produces')
  })
})

describe('llm_step explainer', () => {
  it('shows purpose, model, system and prompt', () => {
    const { container } = renderKind({
      id: 'l1', kind: 'llm_step', purpose: 'classify', model: 'small',
      system: 'You are terse.', prompt: 'Classify {{ steps.fetch.result }}',
    })
    const text = container.textContent!
    expect(text).toContain('classify')
    expect(text).toContain('small')
    expect(text).toContain('You are terse.')
    expect(text).toContain('steps.fetch.result')
  })
})

describe('condition explainer', () => {
  const step: StepDef = {
    id: 'c1', kind: 'condition', when: 'steps.check.result.ok',
    then: [{ id: 'notify', kind: 'tool_call', tool: 'notify' }],
    else: [{ id: 'haltit', kind: 'halt' }],
  }

  it('renders the when expression and both branch step lists', () => {
    const { container } = renderKind(step)
    const text = container.textContent!
    expect(text).toContain('If')
    expect(text).toContain('steps.check.result.ok')
    expect(container.querySelector('[data-testid="branch-then"]')!.textContent).toContain('notify')
    expect(container.querySelector('[data-testid="branch-else"]')!.textContent).toContain('haltit')
  })

  it('clicking a branch step selects that step', () => {
    const onSelect = vi.fn()
    const { container } = renderKind(step, onSelect)
    fireEvent.click(container.querySelector('[data-steplist-id="notify"]')!)
    expect(onSelect).toHaveBeenCalledWith(step.then![0])
  })

  it('renders no else block when else is absent', () => {
    const { container } = renderKind({ ...step, else: undefined })
    expect(container.querySelector('[data-testid="branch-else"]')).toBeNull()
    expect(container.textContent).not.toContain('Otherwise')
  })
})

describe('loop explainer', () => {
  it('renders iteration line, guards, chips, collect and body', () => {
    const { container } = renderKind({
      id: 'lp', kind: 'loop', over: 'steps.list.result', item_name: 'row',
      break_when: 'vars.done', concurrency: 3, max_iterations: 10,
      collect: 'row.name',
      body: [{ id: 'proc', kind: 'code', source: 'return 1' }],
    })
    const text = container.textContent!
    expect(text).toContain('For each')
    expect(text).toContain('row')
    expect(text).toContain('steps.list.result')
    expect(text).toContain('Break when')
    expect(text).toContain('vars.done')
    expect(text).toContain('3 at a time')
    expect(text).toContain('≤ 10 iterations')
    expect(text).toContain('steps.lp.collected')
    expect(container.querySelector('[data-steplist-id="proc"]')).toBeTruthy()
  })

  it('renders no guard rows or chips when absent / default', () => {
    const { container } = renderKind({ id: 'lp', kind: 'loop', over: 'inputs.items', max_iterations: 100 })
    const text = container.textContent!
    expect(text).not.toContain('While')
    expect(text).not.toContain('Until')
    expect(text).not.toContain('iterations')
    expect(text).not.toContain('at a time')
  })

  it('renders a literal list over as a value, not an expression', () => {
    const { container } = renderKind({
      id: 'lp', kind: 'loop', over: ['alpha', 'beta'],
      body: [{ id: 'proc', kind: 'code', source: 'return 1' }],
    })
    const text = container.textContent!
    expect(text).toContain('alpha')
    expect(text).toContain('beta')
  })

  it('renders while/until guards when present', () => {
    const { container } = renderKind({
      id: 'lp', kind: 'loop', over: 'inputs.items',
      while: 'vars.keep_going', until: 'vars.done',
    })
    const text = container.textContent!
    expect(text).toContain('vars.keep_going')
    expect(text).toContain('vars.done')
  })
})

describe('parallel explainer', () => {
  it('renders a card per branch and the fan-in rule', () => {
    const { container } = renderKind({
      id: 'p1', kind: 'parallel', fan_in: 'all',
      branches: [
        [{ id: 'b1s1', kind: 'tool_call', tool: 't1' }],
        [{ id: 'b2s1', kind: 'tool_call', tool: 't2' }],
      ],
    })
    const text = container.textContent!
    expect(text).toContain('Branch 1')
    expect(text).toContain('Branch 2')
    expect(text).toContain('b1s1')
    expect(text).toContain('b2s1')
    expect(text).toContain('all')
  })

  it('renders a fan_in count variant verbatim', () => {
    const { container } = renderKind({
      id: 'p1', kind: 'parallel', fan_in: '2',
      branches: [[{ id: 'b1', kind: 'halt' }], [{ id: 'b2', kind: 'halt' }]],
    })
    expect(container.textContent).toContain('2')
    expect(container.textContent).toContain('branches finish')
  })

  it('renders no fan-in line when fan_in is absent, and empty branches stay empty', () => {
    const { container } = renderKind({
      id: 'p1', kind: 'parallel',
      branches: [[], [{ id: 'only', kind: 'halt' }]],
    })
    const text = container.textContent!
    expect(text).not.toContain('branches finish')
    expect(text).toContain('Branch 1')
    expect(text).toContain('Branch 2')
    expect(text).toContain('only')
  })
})

describe('wait_for_event explainer', () => {
  it('renders event name and filter table', () => {
    const { container } = renderKind({
      id: 'w1', kind: 'wait_for_event', event: 'email.received',
      event_filter: { from: 'boss@co' },
    })
    const text = container.textContent!
    expect(text).toContain('email.received')
    expect(text).toContain('from')
    expect(text).toContain('boss@co')
  })
})

describe('wait_for_approval explainer', () => {
  it('renders one chip per show expression', () => {
    const { container } = renderKind({
      id: 'ap', kind: 'wait_for_approval', show: ['steps.draft.result', 'vars.total'],
    })
    const text = container.textContent!
    expect(text).toContain('steps.draft.result')
    expect(text).toContain('vars.total')
  })

  it('renders nothing without show', () => {
    const { container } = renderKind({ id: 'ap', kind: 'wait_for_approval' })
    expect(container.textContent).toBe('')
  })

  it('renders template-bearing show entries with their refs visible', () => {
    const { container } = renderKind({
      id: 'ap', kind: 'wait_for_approval',
      show: ['Draft: {{ steps.draft.result }}'],
    })
    expect(container.textContent).toContain('steps.draft.result')
  })
})

describe('subtask explainer', () => {
  it('renders playbook name, inputs map and returns map', () => {
    const { container } = renderKind({
      id: 'sub', kind: 'subtask', playbook: 'send_report',
      inputs_map: { report: '{{ steps.build.result }}' },
      returns: { delivered: 'steps.send.result.ok' },
    })
    const text = container.textContent!
    expect(text).toContain('send_report')
    expect(text).toContain('report')
    expect(text).toContain('steps.build.result')
    expect(text).toContain('delivered')
  })
})

describe('state explainer', () => {
  it('renders one row per op with var, value and into', () => {
    const { container } = renderKind({
      id: 'st', kind: 'state',
      state: [
        { op: 'set', var: 'count', value: 0 },
        { op: 'pop_front', var: 'queue', into: 'current' },
      ],
    })
    const rows = container.querySelectorAll('[data-state-op]')
    expect(rows).toHaveLength(2)
    expect(rows[0].textContent).toContain('set')
    expect(rows[0].textContent).toContain('count')
    expect(rows[0].textContent).toContain('0')
    expect(rows[1].textContent).toContain('pop_front')
    expect(rows[1].textContent).toContain('queue')
    expect(rows[1].textContent).toContain('current')
  })

  it('renders a delete op with just the var', () => {
    const { container } = renderKind({
      id: 'st', kind: 'state', state: [{ op: 'delete', var: 'scratch' }],
    })
    const rows = container.querySelectorAll('[data-state-op]')
    expect(rows).toHaveLength(1)
    expect(rows[0].textContent).toContain('delete')
    expect(rows[0].textContent).toContain('scratch')
  })
})

describe('halt explainer', () => {
  it('renders the guard and the returned value', () => {
    const { container } = renderKind({
      id: 'h1', kind: 'halt', when: 'vars.failed', value: { reason: 'quota' },
    })
    const text = container.textContent!
    expect(text).toContain('vars.failed')
    expect(text).toContain('reason')
    expect(text).toContain('quota')
  })

  it('renders no return section without a value', () => {
    const { container } = renderKind({ id: 'h1', kind: 'halt' })
    expect(container.textContent).not.toContain('returns')
  })
})

describe('DataFlow', () => {
  it('renders read and write chips derived from the definition', () => {
    const { container } = render(
      <DataFlow step={{
        id: 'calc', kind: 'code', source: 'return 1',
        code_inputs: { a: '{{ steps.prev.result }}', b: '{{ inputs.n }}' },
      }} />,
    )
    const refs = [...container.querySelectorAll('[data-ref]')].map((el) => el.getAttribute('data-ref'))
    expect(refs).toContain('steps.prev.result')
    expect(refs).toContain('inputs.n')
    expect(refs).toContain('steps.calc.result')
  })

  it('clicking a steps chip jumps to that step id', () => {
    const jump = vi.fn()
    const { container } = render(
      <DataFlow
        step={{ id: 'x', kind: 'tool_call', tool: 't', args: { v: '{{ steps.prev.result }}' } }}
        onJumpToStep={jump}
      />,
    )
    fireEvent.click(container.querySelector('button[data-ref="steps.prev.result"]')!)
    expect(jump).toHaveBeenCalledWith('prev')
  })

  it('renders nothing when the step reads and writes nothing', () => {
    const { container } = render(
      <DataFlow step={{ id: 'h', kind: 'halt' }} />,
    )
    expect(container.querySelector('[data-testid="data-flow"]')).toBeNull()
  })
})

describe('FooterChips', () => {
  it('renders retry, on_error and timeout chips from the definition', () => {
    const { container } = render(
      <FooterChips step={{
        id: 's', kind: 'tool_call', tool: 't',
        retry: { max: 2, backoff_seconds: 5 }, on_error: 'continue', timeout_seconds: 30,
      }} />,
    )
    const text = container.textContent!
    expect(text).toContain('retries 2× (5s backoff)')
    expect(text).toContain('on error: continue')
    expect(text).toContain('timeout 30s')
  })

  it('renders nothing for default error handling', () => {
    const { container } = render(
      <FooterChips step={{ id: 's', kind: 'tool_call', tool: 't', on_error: 'abort' }} />,
    )
    expect(container.querySelector('[data-testid="footer-chips"]')).toBeNull()
  })
})
