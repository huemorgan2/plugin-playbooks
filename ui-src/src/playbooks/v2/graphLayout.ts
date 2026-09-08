/**
 * plans/032 phase 10 — lay the server-derived python graph (`V2Graph`) out
 * as reactflow nodes + edges, and project a run's journal trace onto it.
 *
 * Mirrors `layout.ts` (pblang): top-to-bottom, `if` splits into yes/no
 * columns, loops get the purple loop-back edge, `gather` fans out to a
 * `merge-<id>` node, `try` puts its body in the main column with one
 * handler column per `except`. Node ids are the graph's ids (never
 * positions), so a run overlay and live-patch glow key on them directly.
 */
import type { Node, Edge } from '@xyflow/react'
import type { PlaybookRunDetail, RunStatus, TraceRow } from '../types'
import { lookOf, type V2Block, type V2Graph, type V2Item } from './types'

const NODE_H = 56
const GAP_Y = 60
const GAP_X = 260
const EDGE_STYLE = { stroke: '#475569', strokeWidth: 1.5 }

export interface TraceOverlay {
  /** node id → the status of its LAST occurrence in the run. */
  status: Map<string, RunStatus>
  /** node id → the seq of its last occurrence (drives the fire shimmer). */
  fireSeq: Map<string, number>
  /** node id → every trace row that landed on it, in seq order. */
  rows: Map<string, TraceRow[]>
  /** the node the run-level `failed_line` points at when no effect failed. */
  failedNode: string | null
  running: boolean
}

interface Ctx {
  nodes: Node[]
  edges: Edge[]
  y: number
  overlay?: TraceOverlay
  glow?: Map<string, number>
}

/** Every item of a block in pre-order (args and child blocks after their parent). */
export function flattenItems(block: V2Block): V2Item[] {
  const out: V2Item[] = []
  for (const it of block.items) {
    out.push(it)
    if ('args' in it) out.push(...it.args)
    for (const b of childBlocks(it)) out.push(...flattenItems(b))
  }
  return out
}

function childBlocks(it: V2Item): V2Block[] {
  const blocks: V2Block[] = []
  if (it.kind === 'if') {
    blocks.push(it.then)
    if (it.else) blocks.push(it.else)
  } else if (it.kind === 'for' || it.kind === 'while') {
    blocks.push(it.body)
  } else if (it.kind === 'error_boundary') {
    blocks.push(it.body, ...it.handlers.map((h) => h.body))
    if (it.finally) blocks.push(it.finally)
  }
  return blocks
}

/** The innermost item whose line range covers `line` (mirror of graph.py). */
export function nodeAtLine(graph: V2Graph, line: number): string | null {
  const visit = (block: V2Block): string | null => {
    for (const it of block.items) {
      const hit = visitItem(it)
      if (hit) return hit
    }
    return null
  }
  const visitItem = (it: V2Item): string | null => {
    if (!(it.line <= line && line <= it.end_line)) return null
    if ('args' in it) {
      for (const a of it.args) {
        const hit = visitItem(a)
        if (hit) return hit
      }
    }
    for (const b of childBlocks(it)) {
      const hit = visit(b)
      if (hit) return hit
    }
    return it.node
  }
  return visit(graph.root)
}

export function itemById(graph: V2Graph, id: string): V2Item | null {
  return flattenItems(graph.root).find((it) => it.node === id) ?? null
}

/** Project a run's trace onto node ids. `undefined` when the run has no trace. */
export function overlayTrace(graph: V2Graph, run: PlaybookRunDetail | null): TraceOverlay | undefined {
  if (!run || !run.trace) return undefined
  const status = new Map<string, RunStatus>()
  const fireSeq = new Map<string, number>()
  const rows = new Map<string, TraceRow[]>()
  let anyFailed = false
  let maxSeq = 0
  for (const row of run.trace) {
    status.set(row.node, row.status)
    fireSeq.set(row.node, Math.max(fireSeq.get(row.node) ?? 0, row.seq))
    rows.set(row.node, [...(rows.get(row.node) ?? []), row])
    if (row.status === 'failed') anyFailed = true
    maxSeq = Math.max(maxSeq, row.seq)
  }
  // a gather's status follows its arguments
  for (const it of flattenItems(graph.root)) {
    if (it.kind !== 'gather') continue
    const sts = it.args.map((a) => status.get(a.node)).filter(Boolean) as RunStatus[]
    if (!sts.length) continue
    const st: RunStatus = sts.includes('failed') ? 'failed'
      : sts.includes('running') ? 'running'
      : sts.includes('waiting') ? 'waiting'
      : sts.length === it.args.length ? 'done' : 'running'
    status.set(it.node, st)
    fireSeq.set(it.node, Math.max(...it.args.map((a) => fireSeq.get(a.node) ?? 0)))
  }
  // a pure compute failure: the run-level one-liner names the line
  let failedNode: string | null = null
  if (!anyFailed && run.status === 'failed' && run.failed_line != null) {
    failedNode = nodeAtLine(graph, run.failed_line)
    if (failedNode) {
      status.set(failedNode, 'failed')
      fireSeq.set(failedNode, maxSeq + 1)
    }
  }
  return { status, fireSeq, rows, failedNode, running: run.status === 'running' }
}

/** Ids that are new or changed between two graphs (live-patch glow). Line
 *  numbers are ignored — only the shape of an item counts. */
export function diffGlow(prev: V2Graph | null, next: V2Graph): string[] {
  const shape = (it: V2Item): string => {
    const { line: _l, end_line: _e, ...rest } = it as any
    if ('col' in rest) delete rest.col
    for (const key of ['then', 'else', 'body', 'finally', 'handlers', 'args']) delete rest[key]
    return JSON.stringify(rest)
  }
  const before = new Map<string, string>()
  if (prev) for (const it of flattenItems(prev.root)) before.set(it.node, shape(it))
  const out: string[] = []
  for (const it of flattenItems(next.root)) {
    const b = before.get(it.node)
    if (b === undefined || b !== shape(it)) out.push(it.node)
  }
  return out
}

function pushNode(ctx: Ctx, item: V2Item, x: number, parentId?: string, edgeLabel?: string): string {
  const id = item.node
  const look = lookOf(item.kind)
  const sublabel = 'sublabel' in item ? item.sublabel ?? undefined : undefined
  ctx.nodes.push({
    id,
    type: 'stepNode',
    position: { x, y: ctx.y },
    data: {
      stepId: id,
      kind: look,
      label: item.label,
      sublabel: item.kind === 'compute' ? `${(item as any).lines} line${(item as any).lines === 1 ? '' : 's'}` : sublabel,
      tool: item.kind === 'tool' ? sublabel : undefined,
      v2Item: item,
      runStatus: ctx.overlay?.status.get(id),
      fireSeq: ctx.overlay?.fireSeq.get(id),
      glowSeq: ctx.glow?.get(id),
    },
  })
  if (parentId) pushEdge(ctx, parentId, id, edgeLabel)
  ctx.y += NODE_H + GAP_Y
  return id
}

function pushEdge(ctx: Ctx, source: string, target: string, label?: string, extra?: Partial<Edge>) {
  ctx.edges.push({
    id: `${source}->${target}`,
    source,
    target,
    type: 'smoothstep',
    animated: !!ctx.overlay?.running,
    style: EDGE_STYLE,
    ...(label ? { label } : {}),
    ...extra,
  })
}

function addBlock(ctx: Ctx, block: V2Block, x: number, parentId: string | undefined, firstLabel?: string): string | undefined {
  let last = parentId
  let first = true
  for (const it of block.items) {
    last = addItem(ctx, it, x, last, first ? firstLabel : undefined)
    first = false
  }
  return last
}

function addItem(ctx: Ctx, item: V2Item, x: number, parentId?: string, edgeLabel?: string): string {
  const id = pushNode(ctx, item, x, parentId, edgeLabel)

  if (item.kind === 'if') {
    const branchY = ctx.y
    let lastThen = id
    let thenY = branchY
    if (item.then.items.length) {
      ctx.y = branchY
      lastThen = addBlock(ctx, item.then, x - GAP_X / 2, id, 'yes') ?? id
      thenY = ctx.y
    }
    let lastElse = id
    let elseY = branchY
    if (item.else?.items.length) {
      ctx.y = branchY
      lastElse = addBlock(ctx, item.else, x + GAP_X / 2, id, 'no') ?? id
      elseY = ctx.y
    }
    ctx.y = Math.max(thenY, elseY)
    return item.then.items.length ? lastThen : lastElse
  }

  if (item.kind === 'for' || item.kind === 'while') {
    const lastBody = addBlock(ctx, item.body, x, id) ?? id
    ctx.edges.push({
      id: `${lastBody}->loop-back-${id}`,
      source: lastBody,
      target: id,
      type: 'smoothstep',
      animated: true,
      style: { stroke: '#a855f7', strokeWidth: 1.5 },
      label: 'loop',
    })
    return lastBody
  }

  if (item.kind === 'gather') {
    const branchY = ctx.y
    const n = item.args.length
    const startX = x - (n * GAP_X) / 2 + GAP_X / 2
    const ends: string[] = []
    let maxY = branchY
    for (let i = 0; i < n; i++) {
      ctx.y = branchY
      ends.push(addItem(ctx, item.args[i]!, startX + i * GAP_X, id))
      maxY = Math.max(maxY, ctx.y)
    }
    ctx.y = maxY
    const mergeId = `merge-${id}`
    ctx.nodes.push({
      id: mergeId,
      type: 'stepNode',
      position: { x, y: ctx.y },
      data: { stepId: mergeId, kind: 'parallel', label: 'Merge', sublabel: 'all', runStatus: ctx.overlay?.status.get(id) },
    })
    for (const end of ends) pushEdge(ctx, end, mergeId)
    ctx.y += NODE_H + GAP_Y
    return mergeId
  }

  if (item.kind === 'error_boundary') {
    const branchY = ctx.y
    const ends: string[] = []
    ends.push(addBlock(ctx, item.body, x, id) ?? id)
    let maxY = ctx.y
    item.handlers.forEach((h, i) => {
      ctx.y = branchY
      const hx = x + GAP_X * (i + 1)
      const end = addBlock(ctx, h.body, hx, id, h.label)
      if (end && end !== id) ends.push(end)
      else pushEdge(ctx, id, `merge-${id}`, h.label, { id: `${id}->merge-${id}#${i}` })  // empty handler: straight to the merge
      maxY = Math.max(maxY, ctx.y)
    })
    ctx.y = maxY
    const mergeId = `merge-${id}`
    ctx.nodes.push({
      id: mergeId,
      type: 'stepNode',
      position: { x, y: ctx.y },
      data: { stepId: mergeId, kind: 'error_boundary', label: 'end try', sublabel: item.finally ? 'finally' : undefined },
    })
    for (const end of ends) pushEdge(ctx, end, mergeId)
    ctx.y += NODE_H + GAP_Y
    if (item.finally?.items.length) return addBlock(ctx, item.finally, x, mergeId) ?? mergeId
    return mergeId
  }

  return id
}

export function layoutGraph(
  graph: V2Graph,
  overlay?: TraceOverlay,
  glow?: Map<string, number>,
): { nodes: Node[]; edges: Edge[] } {
  const ctx: Ctx = { nodes: [], edges: [], y: 0, overlay, glow }
  const centerX = 400
  for (let i = 0; i < graph.triggers.length; i++) {
    const trigger = graph.triggers[i]!
    ctx.nodes.push({
      id: `trigger-${i}`,
      type: 'triggerNode',
      position: { x: centerX, y: ctx.y },
      data: {
        event: trigger.event,
        cron: trigger.cron,
        label: trigger.event || trigger.cron || 'Trigger',
        glowSeq: glow?.get(`trigger-${i}`),
      },
    })
    ctx.y += NODE_H + GAP_Y
  }
  const last = ctx.nodes[ctx.nodes.length - 1]?.id
  addBlock(ctx, graph.root, centerX, last)
  return { nodes: ctx.nodes, edges: ctx.edges }
}
