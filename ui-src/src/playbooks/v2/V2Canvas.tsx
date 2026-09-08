/**
 * plans/032 phase 10 — the canvas of a python playbook version. Renders the
 * server-derived graph (`GET /playbooks/{name}/graph`) through the same
 * `CanvasSurface` the pblang canvas uses, with a run's journal trace
 * projected onto the nodes when one is selected.
 */
import { useCallback, useEffect, useMemo } from 'react'
import {
  useNodesState, useEdgesState, type Node, type Edge, type NodeMouseHandler,
} from '@xyflow/react'
import { CanvasSurface } from '../VersionCanvas'
import type { PlaybookRunDetail } from '../types'
import type { V2Graph, V2Item } from './types'
import { layoutGraph, overlayTrace } from './graphLayout'

export function V2Canvas({
  graph,
  name,
  agentName,
  runDetail = null,
  onClearRun,
  onSelectNode,
  glow,
}: {
  graph: V2Graph | null
  name: string
  agentName: string
  runDetail?: PlaybookRunDetail | null
  onClearRun?: () => void
  onSelectNode?: (item: V2Item | null) => void
  /** node id → glow sequence (live agent edits pop the touched node). */
  glow?: Map<string, number>
}) {
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])

  const overlay = useMemo(() => (graph ? overlayTrace(graph, runDetail) : undefined), [graph, runDetail])

  useEffect(() => {
    if (!graph || !graph.node_ids.length) {
      setNodes([])
      setEdges([])
      return
    }
    const { nodes: n, edges: e } = layoutGraph(graph, overlay, glow)
    setNodes(n)
    setEdges(e)
  }, [graph, overlay, glow, setNodes, setEdges])

  const handleNodeClick: NodeMouseHandler = useCallback((_e, node) => {
    const item = (node.data as any)?.v2Item as V2Item | undefined
    if (item) onSelectNode?.(item)
  }, [onSelectNode])

  const failedBanner = runDetail?.error && runDetail.status === 'failed' ? runDetail.error : null

  return (
    <CanvasSurface
      name={graph?.name || name}
      agentName={agentName}
      hasSteps={!!graph?.node_ids.length}
      nodes={nodes}
      edges={edges}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      onNodeClick={handleNodeClick}
      onPaneClick={() => onSelectNode?.(null)}
      runDetail={runDetail}
      onClearRun={onClearRun}
      overlay={failedBanner ? (
        <div
          className="absolute bottom-3 left-3 right-3 z-10 px-3 py-1.5 rounded-lg border border-rose-500/30 bg-ink-950/90 text-[11px] font-mono text-rose-300 truncate"
          data-testid="run-error"
          title={failedBanner}
        >
          {failedBanner}
        </div>
      ) : undefined}
    />
  )
}
