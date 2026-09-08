// @vitest-environment jsdom
// plans/032 phase 10: a python version's Canvas view is the server-derived
// graph (V2Canvas + V2NodePanel); pblang keeps VersionCanvas and never
// calls the graph endpoint.
import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import type { PlaybookDef, PlaybookRunDetail, VersionDetail } from '../types'
import { EXAMPLE_CODE, EXAMPLE_GRAPH } from './v2fixtures'

vi.mock('../api', () => ({
  playbooksApi: {
    listVersions: vi.fn(),
    getVersion: vi.fn(),
    getGraph: vi.fn(),
    promoteVersion: vi.fn(),
    promoteCandidate: vi.fn(),
    listRuns: vi.fn().mockResolvedValue([]),
    getRun: vi.fn(),
  },
}))
vi.mock('../../lib/events', () => ({
  subscribePlaybookEvents: () => () => {},
}))
vi.mock('../ManifestTab', () => ({ ManifestTab: () => <div data-testid="manifest-tab" /> }))
vi.mock('../ConnectionsTab', () => ({ ConnectionsTab: () => <div data-testid="connections-tab" /> }))
vi.mock('../RunsTab', () => ({
  RunsTab: ({ onShowOnCanvas }: { onShowOnCanvas: (id: string) => void }) => (
    <button data-testid="runs-tab" onClick={() => onShowOnCanvas('r1')}>runs</button>
  ),
}))

import { playbooksApi } from '../api'
import { VersionsTab } from '../VersionsTab'

const api = playbooksApi as unknown as Record<string, ReturnType<typeof vi.fn>>

beforeAll(() => {
  class RO { observe() {} unobserve() {} disconnect() {} }
  ;(globalThis as any).ResizeObserver = RO
  ;(globalThis as any).DOMMatrixReadOnly = class { m22 = 1; constructor(_s?: string) {} }
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', { configurable: true, get: () => 500 })
  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', { configurable: true, get: () => 500 })
})
afterEach(cleanup)

// a python definition is the checker summary — no `steps`
const pyDef = {
  name: 'pb', display_name: 'pb', description: '', format: 'python',
  triggers: [{ event: 'manual' }], tools: ['fetch_list', 'send_message'], call_sites: [],
} as unknown as PlaybookDef

const pbDef: PlaybookDef = {
  name: 'greeter', display_name: 'Greeter', description: 'says hi',
  explanation: '', when_to_use: '', agent_autonomy: 'manual', triggers: [],
  steps: [{ id: 'a', kind: 'tool_call', tool: 'send_chat_message', args: { message: 'hi' } }],
}

function detailOf(definition: PlaybookDef, code: string | null, format?: 'pblang' | 'python'): VersionDetail {
  return {
    version: 1, definition, code, manifest: 'M1', author: 'agent',
    message: 'v1 edit', created_at: '2026-09-08T10:00:00Z',
    promoted_from: null, live: true, candidate: false, runs: 1, format,
  }
}

function setup(format: 'pblang' | 'python' | undefined, detail: VersionDetail) {
  api.listVersions.mockResolvedValue([{
    version: 1, title: 'v1 edit', author: 'agent', created_at: '2026-09-08T10:00:00Z',
    runs: 1, promoted_from: null, current: true,
  }])
  api.getVersion.mockResolvedValue(detail)
  api.getGraph.mockResolvedValue(EXAMPLE_GRAPH)
  return render(
    <VersionsTab
      name={detail.definition.name} agentName="Luna" liveVersion={1} candidateVersion={null}
      format={format} onPromoted={() => {}} onManifestSaved={() => {}}
    />,
  )
}

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset?.()
  api.listRuns.mockResolvedValue([])
})

const failedRun: PlaybookRunDetail = {
  id: 'r1', status: 'failed', trigger: 'manual', playbook_version: 1, started_at: null, completed_at: null,
  inputs: {}, steps: [], format: 'python',
  trace: [
    { seq: 1, node: 'step-rows', call_site_id: 'rows', occurrence: 1, kind: 'tool', journal_status: 'done',
      status: 'done', error: null, dry: false, started_at: null, ended_at: null, ms: 3 },
    { seq: 2, node: 'step-send_message', call_site_id: 'send_message', occurrence: 1, kind: 'tool',
      journal_status: 'failed', status: 'failed', error: { type: 'ToolError', message: 'boom' },
      dry: false, started_at: null, ended_at: null, ms: 3 },
  ],
  failed_line: 9, error: 'line 9: … → ToolError: boom after effect send_message#1', error_type: 'ToolError', traceback: null,
} as PlaybookRunDetail

describe('VersionsTab python canvas', () => {
  it('python: fetches the graph for the selected version and renders its nodes', async () => {
    const { container } = setup('python', detailOf(pyDef, EXAMPLE_CODE, 'python'))
    await screen.findByTestId('canvas-name')
    expect(api.getGraph).toHaveBeenCalledWith('pb', 1)
    await waitFor(() => {
      const ids = [...container.querySelectorAll('.react-flow__node')].map((n) => n.getAttribute('data-id'))
      expect(ids).toEqual(EXAMPLE_GRAPH.node_ids)
    })
    expect(screen.queryByTestId('v2-view')).toBeNull()
  })

  it("the detail's own format wins over the prop", async () => {
    const { container } = setup('pblang', detailOf(pyDef, EXAMPLE_CODE, 'python'))
    await screen.findByTestId('canvas-name')
    await waitFor(() => expect(container.querySelectorAll('.react-flow__node').length).toBe(EXAMPLE_GRAPH.node_ids.length))
    expect(api.getGraph).toHaveBeenCalledWith('pb', 1)
  })

  it('pblang: VersionCanvas from the definition, the graph endpoint is never called', async () => {
    const { container } = setup('pblang', detailOf(pbDef, null, 'pblang'))
    await screen.findByTestId('canvas-name')
    expect(container.querySelectorAll('.react-flow__node').length).toBe(1)
    expect(api.getGraph).not.toHaveBeenCalled()
  })

  it('a graph fetch error surfaces as the detail error', async () => {
    setup('python', detailOf(pyDef, EXAMPLE_CODE, 'python'))
    // the graph is fetched only after the version detail resolves
    api.getGraph.mockRejectedValue(new Error('409: {"error":"version 1 of \'pb\' is pblang"}'))
    await screen.findByText(/409/)
    expect(screen.queryByTestId('canvas-name')).toBeNull()
  })

  it('a run shown on canvas colours its nodes; clicking one opens the node panel; Code view keeps the source', async () => {
    api.getRun.mockResolvedValue(failedRun)
    const { container } = setup('python', detailOf(pyDef, EXAMPLE_CODE, 'python'))
    await screen.findByTestId('canvas-name')
    fireEvent.click(screen.getByTestId('view-runs'))
    fireEvent.click(await screen.findByTestId('runs-tab'))
    await screen.findByTestId('run-banner')
    expect(screen.getByTestId('run-error').textContent).toContain('line 9:')
    await waitFor(() => {
      const failed = container.querySelector('.react-flow__node[data-id="step-send_message"]')!
      expect(failed.querySelector('.text-rose-400')).toBeTruthy()
    })
    fireEvent.click(container.querySelector('.react-flow__node[data-id="step-send_message"]')!)
    await screen.findByTestId('v2-node-panel')
    expect(screen.getByTestId('v2-node-id').textContent).toBe('step-send_message')
    expect(screen.getByTestId('v2-node-error').textContent).toBe('boom')
    expect(screen.queryByTestId('step-headline')).toBeNull()
    fireEvent.click(screen.getByTestId('v2-node-close'))
    expect(screen.queryByTestId('v2-node-panel')).toBeNull()
    // Code view: the stored source, never a JSON fallback
    fireEvent.click(screen.getByTestId('view-code'))
    await screen.findByTestId('code-view')
    expect(screen.getByTestId('code-view').textContent).toContain('async def run(ctx, inputs):')
    expect(screen.getByTestId('code-view').textContent).not.toContain('"call_sites"')
  })
})
