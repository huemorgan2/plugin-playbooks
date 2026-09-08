// @vitest-environment jsdom
// plans/032 phase 10: the python canvas renders the server graph through
// CanvasSurface and projects a run's trace onto the nodes.
import { describe, it, expect, vi, beforeAll, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { V2Canvas } from '../v2/V2Canvas'
import { V2NodePanel } from '../v2/V2NodePanel'
import { itemById } from '../v2/graphLayout'
import type { PlaybookRunDetail, TraceRow } from '../types'
import { EXAMPLE_CODE, EXAMPLE_GRAPH, QUEUE_GRAPH } from './v2fixtures'

beforeAll(() => {
  class RO { observe() {} unobserve() {} disconnect() {} }
  ;(globalThis as any).ResizeObserver = RO
  ;(globalThis as any).DOMMatrixReadOnly = class { m22 = 1; constructor(_s?: string) {} }
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', { configurable: true, get: () => 500 })
  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', { configurable: true, get: () => 500 })
})
afterEach(cleanup)

function row(seq: number, id: string, occurrence: number, status: TraceRow['status'], extra: Partial<TraceRow> = {}): TraceRow {
  return {
    seq, node: `step-${id}`, call_site_id: id, occurrence, kind: 'tool', journal_status: status,
    status, error: null, dry: false, started_at: null, ended_at: null, ms: 12, ...extra,
  }
}

const failedRun: PlaybookRunDetail = {
  id: 'r1', status: 'failed', trigger: 'manual', started_at: null, completed_at: null, inputs: {}, steps: [],
  trace: [
    row(1, 'rows', 1, 'done', { args: { url: 'u' }, result: { items: [] } }),
    row(2, 's', 1, 'done'), row(3, 's', 2, 'done'),
    row(4, 'approve', 1, 'done'),
    row(5, 'send_message', 1, 'failed', { error: { type: 'ToolError', message: 'boom' } }),
  ],
  failed_line: 9,
  error: 'line 9: await ctx.tool("send_message", …) → ToolError: boom after effect send_message#1',
  error_type: 'ToolError', traceback: null,
}

describe('V2Canvas', () => {
  it('renders one react-flow node per graph id and the name overlay', () => {
    const { container } = render(<V2Canvas graph={EXAMPLE_GRAPH} name="pb" agentName="Luna" />)
    const ids = [...container.querySelectorAll('.react-flow__node')].map((n) => n.getAttribute('data-id'))
    expect(ids).toEqual(EXAMPLE_GRAPH.node_ids)
    expect(screen.getByTestId('canvas-name').textContent).toContain('pb')
    expect(screen.queryByTestId('run-banner')).toBeNull()
    expect(screen.queryByTestId('run-error')).toBeNull()
  })

  it('merge nodes for gather and try appear beside the graph ids', () => {
    const { container } = render(<V2Canvas graph={QUEUE_GRAPH} name="q" agentName="Luna" />)
    const ids = [...container.querySelectorAll('.react-flow__node')].map((n) => n.getAttribute('data-id'))
    for (const id of QUEUE_GRAPH.node_ids) expect(ids).toContain(id)
    expect(ids).toContain('merge-gather-summary')
    expect(ids).toContain('merge-try-page')
  })

  it('empty graph → the empty-playbook placeholder; null graph → the same', () => {
    render(<V2Canvas graph={{ ...EXAMPLE_GRAPH, node_ids: [], root: { id: 'run', items: [] } }} name="pb" agentName="Luna" />)
    expect(screen.getByText('Empty playbook')).toBeTruthy()
    cleanup()
    render(<V2Canvas graph={null} name="pb" agentName="Luna" />)
    expect(screen.getByText('Empty playbook')).toBeTruthy()
  })

  it('overlays a failed run: banner, failed node colouring, run-error strip', () => {
    const { container } = render(
      <V2Canvas graph={EXAMPLE_GRAPH} name="pb" agentName="Luna" runDetail={failedRun} onClearRun={() => {}} />,
    )
    expect(screen.getByTestId('run-banner').textContent).toContain('Past run')
    expect(screen.getByTestId('run-error').textContent).toContain('line 9:')
    // the status dot wears STATUS_COLORS; a node without a row has no dot
    const node = container.querySelector('.react-flow__node[data-id="step-send_message"]')!
    expect(node.querySelector('.text-rose-400')).toBeTruthy()
    expect(node.querySelector('.node-firing')).toBeTruthy()
    const done = container.querySelector('.react-flow__node[data-id="step-rows"]')!
    expect(done.querySelector('.text-emerald-400')).toBeTruthy()
    const compute = container.querySelector('.react-flow__node[data-id="compute-for-s"]')!
    expect(compute.querySelector('.text-rose-400')).toBeNull()
    expect(compute.querySelector('.text-emerald-400')).toBeNull()
  })

  it('clicking a node reports its item; the pane click clears it', () => {
    const onSelect = vi.fn()
    const { container } = render(
      <V2Canvas graph={EXAMPLE_GRAPH} name="pb" agentName="Luna" onSelectNode={onSelect} />,
    )
    fireEvent.click(container.querySelector('.react-flow__node[data-id="step-s"]')!)
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ node: 'step-s', kind: 'llm', line: 6 }))
  })
})

describe('V2NodePanel', () => {
  it('shows kind, tool, lines and the source slice; without a run no execution block', () => {
    render(<V2NodePanel item={itemById(EXAMPLE_GRAPH, 'step-rows')!} code={EXAMPLE_CODE} run={null} onClose={() => {}} />)
    expect(screen.getByTestId('v2-node-kind').textContent).toBe('Tool')
    expect(screen.getByTestId('v2-node-id').textContent).toBe('step-rows')
    expect(screen.getByTestId('v2-node-tool').textContent).toBe('fetch_list')
    expect(screen.getByTestId('v2-node-lines').textContent).toBe('line 2')
    expect(screen.getByTestId('v2-node-source').textContent).toContain('ctx.tool("fetch_list"')
    expect(screen.getByTestId('v2-node-source').textContent).not.toContain('async def run')
    expect(screen.queryByTestId('v2-node-exec')).toBeNull()
  })

  it('lists every occurrence with the N runs badge, error and raw data', () => {
    render(<V2NodePanel item={itemById(EXAMPLE_GRAPH, 'step-s')!} code={EXAMPLE_CODE} run={failedRun} onClose={() => {}} />)
    expect(screen.getByTestId('v2-node-runs').textContent).toBe('2 runs')
    expect(screen.getAllByTestId('v2-node-occurrence').map((r) => r.getAttribute('data-status'))).toEqual(['done', 'done'])
    cleanup()
    render(<V2NodePanel item={itemById(EXAMPLE_GRAPH, 'step-send_message')!} code={EXAMPLE_CODE} run={failedRun} onClose={() => {}} />)
    expect(screen.queryByTestId('v2-node-runs')).toBeNull()
    expect(screen.getByTestId('v2-node-error').textContent).toBe('boom')
    expect(screen.getByText('Error · ToolError')).toBeTruthy()
    cleanup()
    render(<V2NodePanel item={itemById(EXAMPLE_GRAPH, 'step-rows')!} code={EXAMPLE_CODE} run={failedRun} onClose={() => {}} />)
    fireEvent.click(screen.getByTestId('v2-node-raw-toggle'))
    expect(screen.getAllByTestId('json-tree').length).toBe(2)
  })

  it('a compute node owns the run error when failed_line falls inside it and no effect failed', () => {
    const run: PlaybookRunDetail = {
      ...failedRun, failed_line: 3, trace: [row(1, 'rows', 1, 'done')],
      error: "line 3: rows[\"items\"] → KeyError: 'items' after effect rows#1", traceback: 'tb',
    }
    render(<V2NodePanel item={itemById(EXAMPLE_GRAPH, 'compute-for-s')!} code={EXAMPLE_CODE} run={run} onClose={() => {}} />)
    expect(screen.getByTestId('v2-node-kind').textContent).toBe('Compute')
    expect(screen.getByTestId('v2-node-lines').textContent).toBe('lines 3–4')
    expect(screen.getByTestId('v2-node-run-error').textContent).toContain("KeyError: 'items'")
    expect(screen.getByTestId('v2-node-run-error').textContent).toContain('tb')
    expect(screen.queryByText('Did not run in the selected run.')).toBeNull()
    cleanup()
    render(<V2NodePanel item={itemById(EXAMPLE_GRAPH, 'compute-end-run')!} code={EXAMPLE_CODE} run={run} onClose={() => {}} />)
    expect(screen.queryByTestId('v2-node-run-error')).toBeNull()
    expect(screen.getByText('Did not run in the selected run.')).toBeTruthy()
  })
})
