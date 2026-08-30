/**
 * VersionCanvas (plans/016 phase 3) — the playbook canvas and the read-only
 * code view, lifted out of PlaybookEditor so a version can be rendered
 * anywhere (the Versions tab renders one per selected version).
 *
 *  - `CanvasSurface`  controlled/presentational: overlay, run banner, ReactFlow.
 *  - `VersionCanvas`  owns its graph state for one PlaybookDef (+ optional run).
 *  - `CodeView`       the read-only source block.
 */
import { useCallback, useEffect, useState, type ReactNode } from 'react'
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  BackgroundVariant,
  type Node,
  type Edge,
  type NodeMouseHandler,
  type OnNodesChange,
  type OnEdgesChange,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { Workflow, X, ChevronDown, ChevronRight, Check, Copy } from 'lucide-react'
import { StepNode } from './nodes/StepNode'
import { TriggerNode } from './nodes/TriggerNode'
import { buildGraph } from './layout'
import { Code } from './explain/primitives'
import type { PlaybookDef, PlaybookRunDetail, StepDef } from './types'

export const nodeTypes = {
  stepNode: StepNode,
  triggerNode: TriggerNode,
}

export function CanvasSurface({
  name,
  explanation,
  agentName,
  hasSteps,
  nodes,
  edges,
  onNodesChange,
  onEdgesChange,
  onNodeClick,
  onPaneClick,
  runDetail,
  onClearRun,
  overlay,
}: {
  name: string
  explanation?: string | null
  agentName: string
  hasSteps: boolean
  nodes: Node[]
  edges: Edge[]
  onNodesChange: OnNodesChange<Node>
  onEdgesChange: OnEdgesChange<Edge>
  onNodeClick?: NodeMouseHandler
  onPaneClick?: () => void
  /** A run overlaid on the graph (status colouring) — shows the banner. */
  runDetail?: PlaybookRunDetail | null
  onClearRun?: () => void
  /** Anything floating above the canvas (e.g. a source switch). */
  overlay?: ReactNode
}) {
  const [copied, setCopied] = useState(false)
  const [explainOpen, setExplainOpen] = useState(false)

  if (!hasSteps) {
    return (
      <div className="h-full flex flex-col items-center justify-center text-ink-500 gap-4">
        <Workflow className="w-16 h-16 text-ink-700" />
        <div className="text-center">
          <p className="text-sm font-medium text-ink-300">Empty playbook</p>
          <p className="text-xs text-ink-500 mt-1 max-w-xs">
            Ask {agentName} in chat to build this playbook — steps
            appear here as they are written.
          </p>
        </div>
      </div>
    )
  }

  return (
    <>
      {/* Name + foldable explanation overlay */}
      <div className="absolute top-3 left-3 z-10 max-w-[340px]">
        <span className="inline-flex items-center gap-1.5 text-sm font-mono text-white" data-testid="canvas-name">
          {name}
          <button
            onClick={() => {
              navigator.clipboard?.writeText(name)
              setCopied(true)
              setTimeout(() => setCopied(false), 1500)
            }}
            className="text-ink-500 hover:text-ink-200 transition"
            title="Copy name"
          >
            {copied ? <Check className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
          </button>
        </span>
        {explanation && (
          <div className="mt-1">
            <button
              onClick={() => setExplainOpen(!explainOpen)}
              className="flex items-center gap-1 text-[11px] text-ink-400 hover:text-ink-200 transition"
            >
              {explainOpen ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
              About this playbook
            </button>
            {explainOpen && (
              <p className="mt-1 text-xs text-ink-400 leading-relaxed">
                {explanation}
              </p>
            )}
          </div>
        )}
      </div>
      {overlay}
      {/* Run coloring banner: the canvas is showing a run. */}
      {runDetail && (
        <div
          className="absolute top-3 right-3 z-20 flex items-center gap-1.5 px-2.5 py-1.5 rounded-full border border-amber-500/30 bg-ink-950/90 backdrop-blur-sm shadow-lg text-xs text-amber-100"
          data-testid="run-banner"
        >
          <span className="text-[10px] uppercase tracking-wide text-amber-400/80 font-medium">
            {runDetail.status === 'running' ? 'Live run' : 'Past run'}
          </span>
          {onClearRun && (
            <button
              onClick={onClearRun}
              title="Back to definition"
              className="p-0.5 rounded text-ink-500 hover:text-ink-200 transition"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          )}
        </div>
      )}
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={onNodeClick}
        onPaneClick={onPaneClick}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.3 }}
        proOptions={{ hideAttribution: true }}
        className="bg-ink-950"
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="#1e293b" />
        <Controls
          showInteractive={false}
          className="!bg-ink-900/80 !border-ink-700/50 !rounded-lg !shadow-lg [&>button]:!bg-ink-800 [&>button]:!border-ink-700/50 [&>button]:!text-ink-300 [&>button:hover]:!bg-ink-700"
        />
        {!runDetail && (
          <MiniMap
            className="!bg-ink-900/80 !border-ink-700/50 !rounded-lg"
            nodeColor="#334155"
            maskColor="rgba(0,0,0,0.6)"
          />
        )}
      </ReactFlow>
    </>
  )
}

/** One version's graph, self-contained. Re-layouts when `def` or the
 *  overlaid run changes. */
export function VersionCanvas({
  def,
  name,
  agentName,
  runDetail = null,
  onClearRun,
  onSelectStep,
  overlay,
}: {
  def: PlaybookDef | null
  name: string
  agentName: string
  runDetail?: PlaybookRunDetail | null
  onClearRun?: () => void
  onSelectStep?: (step: StepDef | null) => void
  overlay?: ReactNode
}) {
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])

  useEffect(() => {
    if (!def?.steps?.length) {
      setNodes([])
      setEdges([])
      return
    }
    const { nodes: n, edges: e } = buildGraph(def, runDetail?.steps)
    setNodes(n)
    setEdges(e)
  }, [def, runDetail, setNodes, setEdges])

  const handleNodeClick: NodeMouseHandler = useCallback((_e, node) => {
    const data = node.data as any
    if (data?.stepDef) onSelectStep?.(data.stepDef as StepDef)
  }, [onSelectStep])

  return (
    <CanvasSurface
      name={def?.name || name}
      explanation={def?.explanation}
      agentName={agentName}
      hasSteps={!!def?.steps?.length}
      nodes={nodes}
      edges={edges}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      onNodeClick={handleNodeClick}
      onPaneClick={() => onSelectStep?.(null)}
      runDetail={runDetail}
      onClearRun={onClearRun}
      overlay={overlay}
    />
  )
}

/** Read-only source: pblang when the agent produced it, else the JSON def. */
export function CodeView({
  source,
  agentName,
  header,
}: {
  source: string
  agentName: string
  header?: ReactNode
}) {
  return (
    <div className="h-full p-4 overflow-auto">
      {header}
      <div data-testid="code-view">
        <Code source={source} />
      </div>
      <p className="text-[11px] text-ink-600 mt-2">
        {agentName} writes this — ask in chat to change the playbook.
      </p>
    </div>
  )
}

export function sourceFor(code: string | null | undefined, def: PlaybookDef | null | undefined): string {
  return code || (def ? JSON.stringify(def, null, 2) : '{}')
}
