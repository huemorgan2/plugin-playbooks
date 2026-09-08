// plans/032 phase 10: laying out the server-derived python graph and
// projecting a run's journal trace onto it.
import { describe, it, expect } from 'vitest'
import type { PlaybookRunDetail, TraceRow } from '../types'
import { layoutGraph, overlayTrace, nodeAtLine, diffGlow, flattenItems, itemById } from '../v2/graphLayout'
import { EXAMPLE_GRAPH, QUEUE_GRAPH } from './v2fixtures'

function row(seq: number, id: string, occurrence: number, status: TraceRow['status'], extra: Partial<TraceRow> = {}): TraceRow {
  return {
    seq, node: `step-${id}`, call_site_id: id, occurrence, kind: 'tool',
    journal_status: status === 'done' ? 'done' : status === 'failed' ? 'failed' : status === 'waiting' ? 'parked' : 'in_flight',
    status, error: null, dry: false, started_at: null, ended_at: null, ms: 5, ...extra,
  }
}

function runOf(status: PlaybookRunDetail['status'], trace: TraceRow[] | undefined, extra: Partial<PlaybookRunDetail> = {}): PlaybookRunDetail {
  return { id: 'r1', status, trigger: 'manual', started_at: null, completed_at: null, inputs: {}, steps: [], trace, ...extra }
}

describe('layoutGraph', () => {
  it('one node per graph id (plus merges), trigger first, top-to-bottom, loop-back edge', () => {
    const { nodes, edges } = layoutGraph(EXAMPLE_GRAPH)
    expect(nodes.map((n) => n.id)).toEqual(EXAMPLE_GRAPH.node_ids)
    expect(nodes[0]!.type).toBe('triggerNode')
    expect(nodes.slice(1).every((n) => n.type === 'stepNode')).toBe(true)
    // strictly increasing y down the main column
    const ys = nodes.map((n) => n.position.y)
    expect(ys).toEqual([...ys].sort((a, b) => a - b))
    // looks: tool → tool_call (with the tool name for the icon), llm → llm_step, for → loop, compute → compute
    const data = (id: string) => nodes.find((n) => n.id === id)!.data as any
    expect(data('step-rows').kind).toBe('tool_call')
    expect(data('step-rows').tool).toBe('fetch_list')
    expect(data('step-s').kind).toBe('llm_step')
    expect(data('for-s').kind).toBe('loop')
    expect(data('step-approve').kind).toBe('wait_for_approval')
    expect(data('compute-for-s').kind).toBe('compute')
    expect(data('compute-for-s').sublabel).toBe('2 lines')
    expect(data('compute-end-run').sublabel).toBe('1 line')
    // edges: trigger → rows → compute → for → s → compute-end-for-s, loop back, then approve …
    const ids = edges.map((e) => e.id)
    expect(ids).toContain('trigger-0->step-rows')
    expect(ids).toContain('step-rows->compute-for-s')
    expect(ids).toContain('for-s->step-s')
    expect(ids).toContain('compute-end-for-s->loop-back-for-s')
    expect(edges.find((e) => e.id === 'compute-end-for-s->loop-back-for-s')!.label).toBe('loop')
    expect(ids).toContain('compute-end-for-s->step-approve')
    expect(edges.every((e) => e.animated === false || e.id.includes('loop-back'))).toBe(true)
  })

  it('gather fans out to its args and merges; try puts handlers in side columns', () => {
    const { nodes, edges } = layoutGraph(QUEUE_GRAPH)
    const byId = (id: string) => nodes.find((n) => n.id === id)!
    expect(byId('gather-summary').data.kind).toBe('parallel')
    expect(byId('merge-gather-summary').data.label).toBe('Merge')
    expect(edges.map((e) => e.id)).toContain('gather-summary->step-summary')
    expect(edges.map((e) => e.id)).toContain('step-summary->merge-gather-summary')
    expect(edges.map((e) => e.id)).toContain('merge-gather-summary->compute-step-send_message')
    // try: body in the main column, the except handler one column to the right
    const tryNode = byId('try-page')
    expect(tryNode.data.kind).toBe('error_boundary')
    expect(byId('step-page').position.x).toBe(tryNode.position.x)
    expect(byId('compute-end-try-page-except-1').position.x).toBeGreaterThan(tryNode.position.x)
    const handlerEdge = edges.find((e) => e.id === 'try-page->compute-end-try-page-except-1')!
    expect(handlerEdge.label).toBe('except ctx.ToolError')
    expect(edges.map((e) => e.id)).toContain('step-page->merge-try-page')
    expect(edges.map((e) => e.id)).toContain('compute-end-try-page-except-1->merge-try-page')
    expect(edges.map((e) => e.id)).toContain('merge-try-page->compute-end-while-page')
    expect(edges.map((e) => e.id)).toContain('compute-end-while-page->loop-back-while-page')
    // every node id is unique
    expect(new Set(nodes.map((n) => n.id)).size).toBe(nodes.length)
  })

  it('if splits into yes/no columns', () => {
    const graph = {
      ...EXAMPLE_GRAPH,
      node_ids: ['if-a', 'step-a', 'step-b', 'compute-end-run'],
      root: { id: 'run', items: [
        { node: 'if-a', kind: 'if', label: 'if x', line: 2, end_line: 5,
          then: { id: 'if-a-then', items: [{ node: 'step-a', kind: 'tool', call_site_id: 'a', col: 8, sublabel: 'a', label: 'a', line: 3, end_line: 3, loop_depth: 0, in_try: false }] },
          else: { id: 'if-a-else', items: [{ node: 'step-b', kind: 'tool', call_site_id: 'b', col: 8, sublabel: 'b', label: 'b', line: 5, end_line: 5, loop_depth: 0, in_try: false }] } },
        { node: 'compute-end-run', kind: 'compute', label: 'return', line: 6, end_line: 6, lines: 1 },
      ] },
    } as any
    const { nodes, edges } = layoutGraph(graph)
    const byId = (id: string) => nodes.find((n) => n.id === id)!
    expect(byId('if-a').data.kind).toBe('condition')
    expect(byId('step-a').position.x).toBeLessThan(byId('if-a').position.x)
    expect(byId('step-b').position.x).toBeGreaterThan(byId('if-a').position.x)
    expect(byId('step-a').position.y).toBe(byId('step-b').position.y)
    expect(edges.find((e) => e.id === 'if-a->step-a')!.label).toBe('yes')
    expect(edges.find((e) => e.id === 'if-a->step-b')!.label).toBe('no')
    expect(byId('compute-end-run').position.y).toBeGreaterThan(byId('step-a').position.y)
  })

  it('carries glow onto touched nodes', () => {
    const { nodes } = layoutGraph(EXAMPLE_GRAPH, undefined, new Map([['step-s', 3]]))
    expect(nodes.find((n) => n.id === 'step-s')!.data.glowSeq).toBe(3)
    expect(nodes.find((n) => n.id === 'step-rows')!.data.glowSeq).toBeUndefined()
  })
})

describe('overlayTrace', () => {
  it('is undefined for runs without a trace (pblang / journal-less)', () => {
    expect(overlayTrace(EXAMPLE_GRAPH, null)).toBeUndefined()
    expect(overlayTrace(EXAMPLE_GRAPH, runOf('done', undefined))).toBeUndefined()
  })

  it('last occurrence wins; the failed third send lands on step-send_message', () => {
    const run = runOf('failed', [
      row(1, 'rows', 1, 'done'),
      row(2, 's', 1, 'done'), row(3, 's', 2, 'done'),
      row(4, 'approve', 1, 'done'),
      row(5, 'send_message', 1, 'done'), row(6, 'send_message', 2, 'done'),
      row(7, 'send_message', 3, 'failed', { error: { type: 'ToolError', message: 'boom' } }),
    ], { failed_line: 9 })
    const ov = overlayTrace(EXAMPLE_GRAPH, run)!
    expect(ov.status.get('step-send_message')).toBe('failed')
    expect(ov.status.get('step-s')).toBe('done')
    expect(ov.fireSeq.get('step-send_message')).toBe(7)
    expect(ov.fireSeq.get('step-s')).toBe(3)
    expect(ov.rows.get('step-send_message')!.map((r) => r.occurrence)).toEqual([1, 2, 3])
    expect(ov.failedNode).toBeNull()  // an effect failed — no line projection
    expect(ov.running).toBe(false)
    const { nodes, edges } = layoutGraph(EXAMPLE_GRAPH, ov)
    expect(nodes.find((n) => n.id === 'step-send_message')!.data.runStatus).toBe('failed')
    expect(nodes.find((n) => n.id === 'step-send_message')!.data.fireSeq).toBe(7)
    expect(nodes.find((n) => n.id === 'compute-for-s')!.data.runStatus).toBeUndefined()
    expect(edges.every((e) => e.animated === false || e.id.includes('loop-back'))).toBe(true)
  })

  it('a pure compute failure lands on the compute node covering failed_line', () => {
    const run = runOf('failed', [row(1, 'rows', 1, 'done')], { failed_line: 3, error: "line 3: … → KeyError: 'items' after effect rows#1" })
    const ov = overlayTrace(EXAMPLE_GRAPH, run)!
    expect(ov.failedNode).toBe('compute-for-s')
    expect(ov.status.get('compute-for-s')).toBe('failed')
    expect(ov.fireSeq.get('compute-for-s')).toBe(2)
  })

  it('running runs animate edges; waiting and gather statuses derive', () => {
    const ov = overlayTrace(EXAMPLE_GRAPH, runOf('running', [row(1, 'rows', 1, 'running')]))!
    expect(ov.running).toBe(true)
    expect(layoutGraph(EXAMPLE_GRAPH, ov).edges.every((e) => e.animated)).toBe(true)
    const parked = overlayTrace(EXAMPLE_GRAPH, runOf('waiting', [row(1, 'approve', 1, 'waiting', { parked_on: { kind: 'approval' } })]))!
    expect(parked.status.get('step-approve')).toBe('waiting')
    const g = overlayTrace(QUEUE_GRAPH, runOf('running', [row(1, 'page', 1, 'done'), row(2, 'summary', 1, 'running')]))!
    expect(g.status.get('gather-summary')).toBe('running')
    const g2 = overlayTrace(QUEUE_GRAPH, runOf('done', [row(1, 'summary', 1, 'done')]))!
    expect(g2.status.get('gather-summary')).toBe('done')
    expect(g2.status.get('merge-gather-summary')).toBeUndefined()
    expect(layoutGraph(QUEUE_GRAPH, g2).nodes.find((n) => n.id === 'merge-gather-summary')!.data.runStatus).toBe('done')
  })
})

describe('helpers', () => {
  it('nodeAtLine / itemById / flattenItems mirror the server tree', () => {
    expect(nodeAtLine(EXAMPLE_GRAPH, 6)).toBe('step-s')
    expect(nodeAtLine(EXAMPLE_GRAPH, 7)).toBe('compute-end-for-s')
    expect(nodeAtLine(EXAMPLE_GRAPH, 3)).toBe('compute-for-s')
    expect(nodeAtLine(EXAMPLE_GRAPH, 99)).toBeNull()
    expect(nodeAtLine(QUEUE_GRAPH, 19)).toBe('step-summary')
    expect(nodeAtLine(QUEUE_GRAPH, 10)).toBe('compute-end-try-page-except-1')
    expect(flattenItems(EXAMPLE_GRAPH.root).map((i) => i.node)).toEqual(EXAMPLE_GRAPH.node_ids.slice(1))
    expect(flattenItems(QUEUE_GRAPH.root).map((i) => i.node)).toEqual(QUEUE_GRAPH.node_ids.slice(1))
    expect(itemById(QUEUE_GRAPH, 'step-summary')!.line).toBe(19)
    expect(itemById(QUEUE_GRAPH, 'nope')).toBeNull()
  })

  it('diffGlow: new and reshaped ids glow, line shifts do not', () => {
    expect(diffGlow(null, EXAMPLE_GRAPH)).toEqual(EXAMPLE_GRAPH.node_ids.slice(1))
    const shifted = JSON.parse(JSON.stringify(EXAMPLE_GRAPH)) as typeof EXAMPLE_GRAPH
    for (const it of flattenItems(shifted.root)) { it.line += 2; it.end_line += 2 }
    expect(diffGlow(EXAMPLE_GRAPH, shifted)).toEqual([])
    const changed = JSON.parse(JSON.stringify(EXAMPLE_GRAPH)) as typeof EXAMPLE_GRAPH
    const s = itemById(changed, 'step-s')!
    s.label = 'summary'
    ;(changed.root.items as any[]).push({ node: 'step-new', kind: 'tool', call_site_id: 'new', col: 4, sublabel: 'x', label: 'new', line: 11, end_line: 11, loop_depth: 0, in_try: false })
    expect(diffGlow(EXAMPLE_GRAPH, changed)).toEqual(['step-s', 'step-new'])
  })
})
