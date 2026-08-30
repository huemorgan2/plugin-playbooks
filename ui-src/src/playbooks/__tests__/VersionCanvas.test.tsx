// @vitest-environment jsdom
import { describe, it, expect, vi, beforeAll } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'
import { VersionCanvas, CodeView } from '../VersionCanvas'
import type { PlaybookDef, PlaybookRunDetail } from '../types'

beforeAll(() => {
  // ReactFlow measures itself; jsdom has no layout engine.
  class RO { observe() {} unobserve() {} disconnect() {} }
  ;(globalThis as any).ResizeObserver = RO
  ;(globalThis as any).DOMMatrixReadOnly = class { m22 = 1; constructor(_s?: string) {} }
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', { configurable: true, get: () => 500 })
  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', { configurable: true, get: () => 500 })
})
afterEach(cleanup)

const def: PlaybookDef = {
  name: 'greeter', display_name: 'Greeter', description: 'says hi',
  explanation: 'Greets people.', when_to_use: '', agent_autonomy: 'manual',
  triggers: [],
  steps: [
    { id: 'a', kind: 'tool_call', tool: 'send_chat_message', args: { message: 'hi' } },
    { id: 'b', kind: 'state', state: [{ op: 'set', var: 'x', value: 1 }] },
  ],
}

describe('VersionCanvas', () => {
  it('renders the name overlay and one node per step', () => {
    const { container } = render(<VersionCanvas def={def} name="greeter" agentName="Luna" />)
    expect(screen.getByTestId('canvas-name').textContent).toContain('greeter')
    expect(container.querySelectorAll('.react-flow__node').length).toBe(def.steps.length)
    expect(screen.queryByTestId('run-banner')).toBeNull()
  })

  it('shows the run banner and hides the minimap when a run is overlaid', () => {
    const run: PlaybookRunDetail = {
      id: 'r1', status: 'done', trigger: 'manual', started_at: null, completed_at: null,
      inputs: {}, steps: [{ step_id: 'a', kind: 'tool_call', status: 'done', inputs: null,
        outputs: null, error: null, retry_count: null, cost_cents: null, started_at: null, completed_at: null }],
    }
    const onClear = vi.fn()
    const { container } = render(
      <VersionCanvas def={def} name="greeter" agentName="Luna" runDetail={run} onClearRun={onClear} />,
    )
    expect(screen.getByTestId('run-banner').textContent).toContain('Past run')
    expect(container.querySelector('.react-flow__minimap')).toBeNull()
    fireEvent.click(screen.getByTitle('Back to definition'))
    expect(onClear).toHaveBeenCalled()
  })

  it('shows the empty placeholder for a def without steps', () => {
    render(<VersionCanvas def={{ ...def, steps: [] }} name="greeter" agentName="Luna" />)
    expect(screen.getByText('Empty playbook')).toBeTruthy()
    expect(screen.getByText(/Ask Luna in chat/)).toBeTruthy()
  })
})

describe('CodeView', () => {
  it('renders the source and the agent footer', () => {
    render(<CodeView source={'def run():\n    pass'} agentName="Luna" />)
    expect(screen.getByTestId('code-view').textContent).toContain('def run')
    expect(screen.getByText(/Luna writes this/)).toBeTruthy()
  })
})
