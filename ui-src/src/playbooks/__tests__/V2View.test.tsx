// @vitest-environment jsdom
// plans/032 phase 04: a python playbook's Canvas view is the interim
// "code + journal list" (V2View); pblang keeps VersionCanvas.
import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import type { PlaybookDef, PlaybookRunDetail, VersionDetail } from '../types'

vi.mock('../api', () => ({
  playbooksApi: {
    listVersions: vi.fn(),
    getVersion: vi.fn(),
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
vi.mock('../RunsTab', () => ({ RunsTab: () => <div data-testid="runs-tab" /> }))

import { playbooksApi } from '../api'
import { VersionsTab } from '../VersionsTab'
import { V2View } from '../V2View'

const api = playbooksApi as unknown as Record<string, ReturnType<typeof vi.fn>>

beforeAll(() => {
  class RO { observe() {} unobserve() {} disconnect() {} }
  ;(globalThis as any).ResizeObserver = RO
  ;(globalThis as any).DOMMatrixReadOnly = class { m22 = 1; constructor(_s?: string) {} }
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', { configurable: true, get: () => 500 })
  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', { configurable: true, get: () => 500 })
})
afterEach(cleanup)

const PY_CODE = [
  'async def run(ctx, inputs):',
  '    items = await ctx.tool("fetch_list", url=inputs["url"])',
  '    summaries = []',
  '    for item in items:',
  '        summaries.append(await ctx.tool("send_message", text=item["title"]))',
  '    return {"count": len(summaries)}',
  '',
].join('\n')

// a python definition is the checker summary — no `steps`
const pyDef = {
  name: 'pyx', display_name: 'pyx', description: '', format: 'python',
  triggers: [], tools: ['fetch_list', 'send_message'], call_sites: [],
} as unknown as PlaybookDef

const pbDef: PlaybookDef = {
  name: 'greeter', display_name: 'Greeter', description: 'says hi',
  explanation: '', when_to_use: '', agent_autonomy: 'manual', triggers: [],
  steps: [{ id: 'a', kind: 'tool_call', tool: 'send_chat_message', args: { message: 'hi' } }],
}

function detailOf(definition: PlaybookDef, code: string | null): VersionDetail {
  return {
    version: 1, definition, code, manifest: 'M1', author: 'agent',
    message: 'v1 edit', created_at: '2026-09-08T10:00:00Z',
    promoted_from: null, live: true, candidate: false, runs: 1,
  }
}

function setup(format: 'pblang' | 'python', definition: PlaybookDef, code: string | null) {
  api.listVersions.mockResolvedValue([{
    version: 1, title: 'v1 edit', author: 'agent', created_at: '2026-09-08T10:00:00Z',
    runs: 1, promoted_from: null, current: true,
  }])
  api.getVersion.mockResolvedValue(detailOf(definition, code))
  return render(
    <VersionsTab
      name={definition.name} agentName="Luna" liveVersion={1} candidateVersion={null}
      format={format} onPromoted={() => {}} onManifestSaved={() => {}}
    />,
  )
}

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset?.()
  api.listRuns.mockResolvedValue([])
})

const run: PlaybookRunDetail = {
  id: 'r1', status: 'failed', trigger: 'manual', started_at: null, completed_at: null,
  inputs: {},
  steps: [
    { step_id: 'items#1', kind: 'tool', status: 'done', inputs: null, outputs: null,
      error: null, retry_count: null, cost_cents: null, started_at: null, completed_at: null },
    { step_id: 'log#1', kind: 'log', status: 'done', inputs: null, outputs: null,
      error: null, retry_count: null, cost_cents: null, started_at: null, completed_at: null },
    { step_id: 'summaries#1', kind: 'tool', status: 'failed', inputs: null, outputs: null,
      error: "line 5: ... → KeyError: 'title' after effect items#1",
      retry_count: null, cost_cents: null, started_at: null, completed_at: null },
  ],
}

describe('V2View', () => {
  it('renders the header, the source and no graph', () => {
    const { container } = render(<V2View code={PY_CODE} runDetail={null} agentName="Luna" />)
    expect(screen.getByTestId('v2-header').textContent)
      .toBe('python playbook — the graph view arrives with the canvas phase')
    expect(screen.getByTestId('v2-code').textContent).toContain('async def run(ctx, inputs):')
    expect(screen.getByTestId('v2-code').textContent).toContain('ctx.tool("fetch_list"')
    expect(container.querySelectorAll('.react-flow__node').length).toBe(0)
    expect(screen.queryAllByTestId('v2-step').length).toBe(0)
  })

  it('lists the run steps in order with id, kind, status and the failed row error', () => {
    render(<V2View code={PY_CODE} runDetail={run} agentName="Luna" />)
    const rows = screen.getAllByTestId('v2-step')
    expect(rows.length).toBe(3)
    expect(rows.map((r) => r.querySelector('[data-testid="v2-step-id"]')?.textContent))
      .toEqual(['items#1', 'log#1', 'summaries#1'])
    expect(rows.map((r) => r.querySelector('[data-testid="v2-step-kind"]')?.textContent))
      .toEqual(['tool', 'log', 'tool'])
    expect(rows.map((r) => r.querySelector('[data-testid="v2-step-status"]')?.textContent))
      .toEqual(['done', 'done', 'failed'])
    expect(rows[0].querySelector('[data-testid="v2-step-error"]')).toBeNull()
    expect(rows[2].querySelector('[data-testid="v2-step-error"]')?.textContent)
      .toBe("line 5: ... → KeyError: 'title' after effect items#1")
  })
})

describe('VersionsTab format switch', () => {
  it('python: the Canvas view is V2View with the stored source and zero react-flow nodes', async () => {
    const { container } = setup('python', pyDef, PY_CODE)
    await screen.findByTestId('v2-code')
    expect(screen.getByTestId('v2-code').textContent).toContain('async def run(ctx, inputs):')
    expect(container.querySelectorAll('.react-flow__node').length).toBe(0)
    expect(screen.queryByTestId('canvas-name')).toBeNull()
    // never sourceFor's JSON fallback
    expect(screen.getByTestId('v2-code').textContent).not.toContain('"call_sites"')
  })

  it('pblang: the Canvas view is still VersionCanvas', async () => {
    const { container } = setup('pblang', pbDef, null)
    await screen.findByTestId('canvas-name')
    expect(screen.queryByTestId('v2-code')).toBeNull()
    expect(container.querySelectorAll('.react-flow__node').length).toBe(pbDef.steps.length)
  })
})
