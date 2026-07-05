import { useCallback, useEffect, useRef, useState } from 'react'
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
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import {
  ArrowLeft, Play, Square, FileCode, Eye, Loader2, Rocket, Workflow,
  X, ChevronDown, ChevronRight, Settings, History,
  Bot, Wrench, GitBranch, Layers, Clock, Mail, RotateCcw, ExternalLink, Zap,
  ShieldCheck, ShieldAlert, ShieldOff, Check, ArrowUpCircle, Copy, Sparkles,
  Database, Ban,
} from 'lucide-react'
import { cn } from '../lib/cn'
import { StepNode } from './nodes/StepNode'
import { TriggerNode } from './nodes/TriggerNode'
import { buildGraph } from './layout'
import { applyPlaybookPatch, patchMatchesEditor, type PlaybookPatchEvt } from './livePatch'
import { setPlaybookConsumerReady } from './liveBus'
import { playbooksApi } from './api'
import type {
  PlaybookDef, StepDef, StepKind, StepRunDetail,
  PlaybookRunDetail,
} from './types'
import { STEP_COLORS } from './types'
import { PlaybookRuns } from './PlaybookRuns'
import { StateVizPanel } from './StateVizPanel'
import { buildTimeline, hasState, type TimelineFrame } from './runReplay'

const nodeTypes = {
  stepNode: StepNode,
  triggerNode: TriggerNode,
}

type ViewMode = 'canvas' | 'yaml'

type Props =
  | { name: string; draftId?: undefined; onBack: () => void }
  | { name?: undefined; draftId: string; onBack: () => void }

const KIND_ICONS: Record<StepKind, React.ComponentType<{ className?: string }>> = {
  agent_step: Bot,
  llm_step: Sparkles,
  tool_call: Wrench,
  condition: GitBranch,
  parallel: Layers,
  wait_for_approval: Clock,
  wait_for_event: Mail,
  subtask: ExternalLink,
  loop: RotateCcw,
  state: Database,
  halt: Ban,
}

function fmtStateOp(o: { op: string; var: string; value?: any; into?: string }): string {
  const v =
    o.value === undefined ? ''
      : typeof o.value === 'string' ? ` = ${o.value}`
      : ` = ${JSON.stringify(o.value)}`
  const into = o.into ? ` → ${o.into}` : ''
  return `${o.op} ${o.var}${v}${into}`
}

// 007.009.01: surface EVERY relevant StepDef field so a node click is a complete
// static spec — the half-populated panel was a real debugging gap.
function stepDetailRows(step: StepDef): { label: string; value: string }[] {
  const rows: { label: string; value: string }[] = []
  if (step.tool) rows.push({ label: 'Tool', value: step.tool })
  if (step.args) rows.push({ label: 'Args', value: JSON.stringify(step.args, null, 2) })
  if (step.prompt) rows.push({ label: 'Prompt', value: step.prompt })
  if (step.system) rows.push({ label: 'System', value: step.system })
  if (step.purpose) rows.push({ label: 'Purpose', value: step.purpose })
  if (step.model) rows.push({ label: 'Model', value: step.model })
  if (step.output_schema) rows.push({ label: 'Output schema', value: JSON.stringify(step.output_schema, null, 2) })
  if (step.tools?.length) rows.push({ label: 'Tools', value: step.tools.join(', ') })
  if (step.when) rows.push({ label: 'Condition', value: step.when })
  if (step.event) rows.push({ label: 'Event', value: step.event })
  if (step.event_filter) rows.push({ label: 'Event filter', value: JSON.stringify(step.event_filter, null, 2) })
  if (step.playbook) rows.push({ label: 'Subtask', value: step.playbook })
  if (step.inputs_map) rows.push({ label: 'Inputs map', value: JSON.stringify(step.inputs_map, null, 2) })
  if (step.returns) rows.push({ label: 'Returns', value: JSON.stringify(step.returns, null, 2) })
  if (step.over) rows.push({ label: 'Loop over', value: step.over })
  if (step.while) rows.push({ label: 'While', value: step.while })
  if (step.until) rows.push({ label: 'Until', value: step.until })
  if (step.break_when) rows.push({ label: 'Break when', value: step.break_when })
  if (step.concurrency && step.concurrency > 1) rows.push({ label: 'Concurrency', value: String(step.concurrency) })
  if (step.collect) rows.push({ label: 'Collect', value: step.collect })
  if (step.item_name) rows.push({ label: 'Item name', value: step.item_name })
  if (step.max_iterations) rows.push({ label: 'Max iterations', value: String(step.max_iterations) })
  if (step.state?.length) rows.push({ label: 'State ops', value: step.state.map(fmtStateOp).join('\n') })
  if (step.kind === 'halt' && step.value !== undefined)
    rows.push({ label: 'Return value', value: typeof step.value === 'string' ? step.value : JSON.stringify(step.value, null, 2) })
  if (step.fan_in) rows.push({ label: 'Fan-in', value: step.fan_in })
  if (step.branches?.length) rows.push({ label: 'Branches', value: `${step.branches.length} parallel paths` })
  if (step.timeout_seconds) rows.push({ label: 'Timeout', value: `${step.timeout_seconds}s` })
  if (step.on_error && step.on_error !== 'abort') rows.push({ label: 'On error', value: step.on_error })
  if (step.retry?.max) rows.push({ label: 'Retry', value: `${step.retry.max}x, ${step.retry.backoff_seconds}s backoff` })
  if (step.show?.length) rows.push({ label: 'Show', value: step.show.join(', ') })
  return rows
}

// All run-detail rows for one step id (a loop body step runs once per iteration).
function execRowsForStep(run: PlaybookRunDetail | null, stepId: string): StepRunDetail[] {
  if (!run) return []
  return run.steps.filter((s) => s.step_id === stepId)
}

// 006.714: one plain-English sentence describing what a step DID in a run, so a
// human reads the outcome instead of decoding JSON. Raw data stays one click away.
function execSummary(step: StepDef, exec: StepRunDetail): string {
  const kind = step.kind
  if (exec.status === 'failed') {
    const first = (exec.error || '').split('\n')[0]?.trim()
    return first ? `Failed: ${first}` : 'This step failed.'
  }
  if (exec.status === 'running') return 'Running now…'
  if (exec.status === 'waiting') {
    return kind === 'wait_for_approval' ? 'Waiting for approval.' : 'Waiting for an event.'
  }
  if (exec.status === 'cancelled') return 'This step was cancelled.'
  // succeeded
  switch (kind) {
    case 'tool_call':
      return step.tool ? `Called \`${step.tool}\` — succeeded.` : 'Ran a tool — succeeded.'
    case 'agent_step':
      return 'The agent ran and produced a result.'
    case 'llm_step':
      return 'Generated a result.'
    case 'condition': {
      const branch = exec.outputs?.branch ?? exec.outputs?.taken
      return branch ? `Condition took the \`${branch}\` branch.` : 'Condition was evaluated.'
    }
    case 'loop': {
      const n = exec.outputs?.iterations
      const stopped = exec.outputs?.stopped
      const base = n != null ? `Ran ${n} iteration${Number(n) === 1 ? '' : 's'}.` : 'Looped over the items.'
      return stopped ? `${base} (stopped: ${stopped})` : base
    }
    case 'parallel':
      return 'Ran its parallel branches.'
    case 'subtask':
      return step.playbook ? `Ran the \`${step.playbook}\` sub-playbook.` : 'Ran a sub-playbook.'
    case 'state':
      return 'Updated the run state.'
    case 'wait_for_approval':
      return 'Approval was granted.'
    case 'wait_for_event':
      return 'The awaited event arrived.'
    case 'halt':
      return 'Ended the run early (success).'
    default:
      return 'Completed.'
  }
}

export function PlaybookEditor(props: Props) {
  const { onBack } = props

  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [definition, setDefinition] = useState<PlaybookDef | null>(null)
  const [meta, setMeta] = useState<{
    display_name: string
    status: string
    version: number
    isDraft: boolean
    draftId?: string
  } | null>(null)
  const [viewMode, setViewMode] = useState<ViewMode>('canvas')
  const [yamlText, setYamlText] = useState('')
  const [promoting, setPromoting] = useState(false)
  const [selectedStep, setSelectedStep] = useState<StepDef | null>(null)
  const [explainOpen, setExplainOpen] = useState(false)
  const [copied, setCopied] = useState(false)
  const [autonomy, setAutonomy] = useState<string>('agent_must_confirm')
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [versionsOpen, setVersionsOpen] = useState(false)
  const [runsOpen, setRunsOpen] = useState(false)

  // 007.009.01 + viz: run-replay state for the canvas. A selected run drives the
  // node-fire shimmer + the stack/queue panel from one cursor.
  const [runDetail, setRunDetail] = useState<PlaybookRunDetail | null>(null)
  const [timeline, setTimeline] = useState<TimelineFrame[]>([])
  const [cursor, setCursor] = useState(-1)
  const [playing, setPlaying] = useState(false)

  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])

  const fireSeqRef = useRef(0)
  const fireNode = useCallback((nodeId: string) => {
    fireSeqRef.current += 1
    const seq = fireSeqRef.current
    setNodes((prev) => prev.map((n) => (n.id === nodeId ? { ...n, data: { ...n.data, fireSeq: seq } } : n)))
  }, [setNodes])

  // 006.709: live patches from the agent. defRef mirrors `definition` so the
  // sequential queue can read-modify-write without stale closures.
  const defRef = useRef<PlaybookDef | null>(null)
  useEffect(() => { defRef.current = definition }, [definition])
  const queueRef = useRef<PlaybookPatchEvt[]>([])
  const drainingRef = useRef(false)
  const glowSeqRef = useRef(0)
  const glowMapRef = useRef(new Map<string, number>())

  const loadData = useCallback(() => {
    const load = props.draftId
      ? playbooksApi.getDraft(props.draftId).then((d) => {
          const def = d.definition as PlaybookDef
          setDefinition(def)
          defRef.current = def
          setMeta({
            display_name: def.display_name || d.name,
            status: 'draft',
            version: 0,
            isDraft: true,
            draftId: d.id,
          })
          setYamlText(JSON.stringify(def, null, 2))
          if (def.steps?.length) {
            const { nodes: n, edges: e } = buildGraph(def)
            setNodes(n)
            setEdges(e)
          }
        })
      : playbooksApi.get(props.name!).then((pb) => {
          const def = pb.definition as PlaybookDef
          setDefinition(def)
          defRef.current = def
          setAutonomy(pb.agent_autonomy)
          setMeta({
            display_name: pb.display_name,
            status: pb.status as string,
            version: pb.version,
            isDraft: false,
          })
          setYamlText(JSON.stringify(def, null, 2))
          const { nodes: n, edges: e } = buildGraph(def)
          setNodes(n)
          setEdges(e)
        })

    load.catch((e) => setError(e.message)).finally(() => setLoading(false))
  }, [props.draftId, props.name, setNodes, setEdges])

  useEffect(() => { loadData() }, [loadData])

  // --- Run replay (canvas) -------------------------------------------------
  // Load a run's detail, color the graph by status, build the frame timeline,
  // and (optionally) auto-play the node-fire shimmer + state viz.
  const loadCanvasRun = useCallback(async (runId: string, autoplay = false) => {
    try {
      const detail = await playbooksApi.getRun(runId)
      const def = defRef.current
      setRunDetail(detail)
      const tl = buildTimeline(detail)
      setTimeline(tl)
      if (def) {
        const { nodes: n, edges: e } = buildGraph(def, detail.steps)
        setNodes(n)
        setEdges(e)
      }
      setCursor(-1)
      setPlaying(autoplay && tl.length > 0)
    } catch { /* ignore */ }
  }, [setNodes, setEdges])

  // 007.013-B: play a run picked from the Runs list — switch to the canvas so
  // the glow is visible, then load + autoplay it.
  const playRunFromList = useCallback((runId: string) => {
    setViewMode('canvas')
    loadCanvasRun(runId, true)
  }, [loadCanvasRun])

  // 006.714: load a run onto the canvas WITHOUT animating — colors the graph by
  // status so the owner can inspect steps. Replay only starts on an explicit Play.
  const loadRunStatic = useCallback((runId: string) => {
    setViewMode('canvas')
    loadCanvasRun(runId, false)
  }, [loadCanvasRun])

  // 006.714: no auto-play. Opening a playbook shows the clean definition; a past
  // run only renders when the owner picks one from the Runs panel (and only
  // animates when they hit Play). The old auto-replay made it look like the
  // playbook was running live on every open.

  // Replay clock: advance the cursor one frame at a time while playing.
  // 007.013-B: fixed cadence — the scrubber/speed control was removed; replay
  // is now just play/stop driven from the Runs list.
  useEffect(() => {
    if (!playing) return
    if (cursor >= timeline.length - 1) { setPlaying(false); return }
    const t = setTimeout(() => setCursor((c) => c + 1), 680)
    return () => clearTimeout(t)
  }, [playing, cursor, timeline.length])

  // Fire the node at the cursor (trigger first, then each step in order).
  const timelineRef = useRef<TimelineFrame[]>([])
  useEffect(() => { timelineRef.current = timeline }, [timeline])
  useEffect(() => {
    if (!runDetail) return
    if (cursor < 0) { fireNode('trigger-0'); return }
    const frame = timelineRef.current[cursor]
    if (frame) fireNode(`step-${frame.stepId}`)
  }, [cursor, runDetail, fireNode])

  // Near-live: poll a running run and refresh statuses without a layout rebuild
  // (so the shimmer keeps moving on the same node objects).
  useEffect(() => {
    if (!runDetail || runDetail.status !== 'running') return
    const t = setTimeout(async () => {
      try {
        const fresh = await playbooksApi.getRun(runDetail.id)
        setRunDetail(fresh)
        setTimeline(buildTimeline(fresh))
        const byStep = new Map(fresh.steps.map((s) => [s.step_id, s.status]))
        setNodes((prev) => prev.map((n) => {
          const sid = (n.data as any).stepId as string | undefined
          const st = sid ? byStep.get(sid) : undefined
          return st ? { ...n, data: { ...n.data, runStatus: st } } : n
        }))
      } catch { /* ignore */ }
    }, 1400)
    return () => clearTimeout(t)
  }, [runDetail, setNodes])

  // Apply ONE patch: mutate the definition, rebuild the graph, mark the
  // affected node so it pops in with a glow in its kind color.
  const applyOnePatch = useCallback((evt: PlaybookPatchEvt) => {
    if (evt.action === 'replace') {
      // Draft was saved as a live playbook — switch the section to it.
      if (props.draftId && evt.name) {
        window.dispatchEvent(
          new CustomEvent('luna:playbook-open', { detail: { draft_id: evt.name } }),
        )
      } else {
        loadData()
      }
      return
    }
    const cur = defRef.current
    if (!cur) return
    // The agent is editing the playbook — drop any run replay so build-glow and
    // run-shimmer don't fight over the same nodes.
    if (defRef.current) {
      setRunDetail(null)
      setTimeline([])
      setPlaying(false)
      setCursor(-1)
    }
    const { def: nextDef, glowNodeId } = applyPlaybookPatch(cur, evt)
    defRef.current = nextDef
    setDefinition(nextDef)
    setYamlText(JSON.stringify(nextDef, null, 2))
    if (glowNodeId) {
      glowSeqRef.current += 1
      glowMapRef.current.set(glowNodeId, glowSeqRef.current)
    }
    const { nodes: n, edges: e } = buildGraph(nextDef)
    setNodes(
      n.map((node) => {
        const seq = glowMapRef.current.get(node.id)
        return seq ? { ...node, data: { ...node.data, glowSeq: seq } } : node
      }),
    )
    setEdges(e)
  }, [loadData, props.draftId, setNodes, setEdges])
  const applyOnePatchRef = useRef(applyOnePatch)
  useEffect(() => { applyOnePatchRef.current = applyOnePatch }, [applyOnePatch])

  // Shared staggered queue: several rapid changes appear one-by-one, 500ms
  // apart — additions AND edits ride the same queue.
  const drainQueue = useCallback(() => {
    if (drainingRef.current) return
    drainingRef.current = true
    const step = () => {
      const evt = queueRef.current[0]
      if (!evt) {
        drainingRef.current = false
        return
      }
      // Initial load still in flight — hold the queue until the def exists.
      if (!defRef.current && evt.action !== 'replace') {
        setTimeout(step, 200)
        return
      }
      queueRef.current.shift()
      applyOnePatchRef.current(evt)
      setTimeout(step, 500)
    }
    step()
  }, [])

  useEffect(() => {
    const onPatch = (e: Event) => {
      const evt = (e as CustomEvent).detail as PlaybookPatchEvt
      if (!patchMatchesEditor(evt, props.draftId, props.name)) return
      queueRef.current.push(evt)
      drainQueue()
    }
    window.addEventListener('luna:playbook-patch', onPatch)
    setPlaybookConsumerReady('patch', true) // replay patches missed while unmounted
    return () => {
      setPlaybookConsumerReady('patch', false)
      window.removeEventListener('luna:playbook-patch', onPatch)
    }
  }, [props.draftId, props.name, drainQueue])

  const handleNodeClick: NodeMouseHandler = useCallback((_event, node) => {
    const data = node.data as any
    if (data?.stepDef) {
      setSelectedStep(data.stepDef as StepDef)
    }
  }, [])

  const handlePaneClick = useCallback(() => {
    setSelectedStep(null)
  }, [])

  const handlePromote = async () => {
    if (!meta?.draftId || promoting) return
    setPromoting(true)
    try {
      await playbooksApi.promoteDraft(meta.draftId)
      onBack()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setPromoting(false)
    }
  }

  const changeAutonomy = async (value: string) => {
    if (!props.name || value === autonomy) return
    try {
      await playbooksApi.setAutonomy(props.name, value)
      setAutonomy(value)
    } catch {}
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full text-ink-400 gap-2">
        <Loader2 className="w-5 h-5 animate-spin" />
        Loading…
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-ink-400 gap-3">
        <p className="text-rose-400 text-sm">{error}</p>
        <button onClick={onBack} className="text-sm text-luna-400 hover:underline">
          Back to list
        </button>
      </div>
    )
  }

  const isDraft = meta?.isDraft
  const hasSteps = !!(definition?.steps?.length)
  const displayName = meta?.display_name || props.name || 'Untitled'
  const playbookExplanation = definition?.explanation

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="flex items-center gap-3 px-4 py-3 border-b border-white/5 shrink-0">
        <button
          onClick={onBack}
          className="p-1.5 rounded-lg hover:bg-white/5 text-ink-400 hover:text-ink-100 transition"
        >
          <ArrowLeft className="w-4 h-4" />
        </button>
        <div className="flex-1 min-w-0">
          <h2 className="text-sm font-semibold text-ink-100 truncate">
            {displayName}
          </h2>
          <div className="flex items-center gap-2 text-[11px] text-ink-500">
            {isDraft ? (
              <span className="px-1.5 py-0.5 rounded text-[10px] font-medium bg-amber-900/40 text-amber-400">
                draft
              </span>
            ) : (
              <>
                <span>v{meta?.version}</span>
                <span
                  className={cn(
                    'px-1.5 py-0.5 rounded text-[10px] font-medium',
                    meta?.status === 'enabled'
                      ? 'bg-emerald-900/40 text-emerald-400'
                      : 'bg-ink-800 text-ink-400',
                  )}
                >
                  {meta?.status}
                </span>
              </>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          {!isDraft && (
            <>
              <button
                onClick={() => { setRunsOpen((v) => !v); setVersionsOpen(false); setSettingsOpen(false) }}
                className={cn(
                  'p-1.5 rounded-lg transition',
                  runsOpen
                    ? 'bg-luna-600/30 text-luna-200'
                    : 'hover:bg-white/5 text-ink-400 hover:text-ink-100',
                )}
                title="Run history"
              >
                <Play className="w-4 h-4" />
              </button>
              <button
                onClick={() => { setVersionsOpen(!versionsOpen); setSettingsOpen(false); setRunsOpen(false) }}
                className={cn(
                  'p-1.5 rounded-lg transition',
                  versionsOpen
                    ? 'bg-luna-600/30 text-luna-200'
                    : 'hover:bg-white/5 text-ink-400 hover:text-ink-100',
                )}
                title="Version history"
              >
                <History className="w-4 h-4" />
              </button>
              <button
                onClick={() => { setSettingsOpen(!settingsOpen); setVersionsOpen(false); setRunsOpen(false) }}
                className={cn(
                  'p-1.5 rounded-lg transition',
                  settingsOpen
                    ? 'bg-luna-600/30 text-luna-200'
                    : 'hover:bg-white/5 text-ink-400 hover:text-ink-100',
                )}
                title="Playbook settings"
              >
                <Settings className="w-4 h-4" />
              </button>
            </>
          )}
          {isDraft && (
            <button
              onClick={handlePromote}
              disabled={promoting || !hasSteps}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 text-white text-xs font-medium transition"
              title={!hasSteps ? 'Add steps before promoting' : 'Promote to live playbook'}
            >
              {promoting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Rocket className="w-3.5 h-3.5" />}
              Promote
            </button>
          )}
          <div className="flex items-center gap-1 bg-ink-900/60 rounded-lg p-0.5">
            <TabBtn active={viewMode === 'canvas'} onClick={() => setViewMode('canvas')}>
              <Eye className="w-3.5 h-3.5" /> Canvas
            </TabBtn>
            <TabBtn active={viewMode === 'yaml'} onClick={() => setViewMode('yaml')}>
              <FileCode className="w-3.5 h-3.5" /> YAML
            </TabBtn>
          </div>
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 min-h-0 relative flex">
        <div className="flex-1 min-w-0 relative">
          {viewMode === 'canvas' && (
            <>
              {hasSteps ? (
                <>
                  {/* Name + foldable explanation overlay */}
                  <div className="absolute top-3 left-3 z-10 max-w-[340px]">
                    <span className="inline-flex items-center gap-1.5 text-sm font-mono text-white">
                      {definition?.name || props.name}
                      <button
                        onClick={() => {
                          navigator.clipboard.writeText(definition?.name || props.name || '')
                          setCopied(true)
                          setTimeout(() => setCopied(false), 1500)
                        }}
                        className="text-ink-500 hover:text-ink-200 transition"
                        title="Copy name"
                      >
                        {copied ? <Check className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
                      </button>
                    </span>
                    {playbookExplanation && (
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
                            {playbookExplanation}
                          </p>
                        )}
                      </div>
                    )}
                  </div>
                  {/* 007.013-B: replay reduced to a single play/stop toggle.
                      Run selection + stats live in the Runs tab. */}
                  {!isDraft && runDetail && timeline.length > 0 && (
                    <ReplayToggle
                      playing={playing}
                      startedAt={runDetail.started_at}
                      trigger={runDetail.trigger}
                      onToggle={() => {
                        if (timeline.length === 0) return
                        if (cursor >= timeline.length - 1) setCursor(-1)
                        setPlaying((p) => !p)
                      }}
                      onClear={() => {
                        setPlaying(false)
                        setRunDetail(null)
                        setTimeline([])
                        setCursor(-1)
                        if (defRef.current) {
                          const { nodes: n, edges: e } = buildGraph(defRef.current)
                          setNodes(n)
                          setEdges(e)
                        }
                      }}
                    />
                  )}
                  <ReactFlow
                    nodes={nodes}
                    edges={edges}
                    onNodesChange={onNodesChange}
                    onEdgesChange={onEdgesChange}
                    onNodeClick={handleNodeClick}
                    onPaneClick={handlePaneClick}
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
                  {/* 007.009.01 (viz): live stack/queue/set/counter, synced to the
                      same replay cursor as the node shimmer. */}
                  {runDetail && hasState(timeline) && (
                    <StateVizPanel timeline={timeline} idx={cursor} />
                  )}
                </>
              ) : (
                <div className="h-full flex flex-col items-center justify-center text-ink-500 gap-4">
                  <Workflow className="w-16 h-16 text-ink-700" />
                  <div className="text-center">
                    <p className="text-sm font-medium text-ink-300">Empty playbook</p>
                    <p className="text-xs text-ink-500 mt-1 max-w-xs">
                      Switch to the YAML tab to define steps, or ask Luna in chat
                      to help build this playbook.
                    </p>
                  </div>
                </div>
              )}
            </>
          )}

          {viewMode === 'yaml' && (
            <div className="h-full p-4 overflow-auto">
              <pre className="text-xs text-ink-300 font-mono whitespace-pre-wrap bg-ink-900/40 rounded-lg p-4 border border-white/5">
                {yamlText || '{}'}
              </pre>
            </div>
          )}

        </div>

        {/* Runs side panel (006.714): list opens on the side, picking a run
            colors the canvas; per-step detail comes from clicking a node. */}
        {runsOpen && !isDraft && props.name && (
          <PlaybookRuns
            name={props.name}
            activeRunId={runDetail?.id ?? null}
            onLoadRun={loadRunStatic}
            onPlayRun={playRunFromList}
            onClose={() => setRunsOpen(false)}
          />
        )}

        {/* Step detail panel */}
        {selectedStep && viewMode === 'canvas' && !settingsOpen && (
          <StepDetailPanel
            step={selectedStep}
            execRows={execRowsForStep(runDetail, selectedStep.id)}
            hasRun={!!runDetail}
            onClose={() => setSelectedStep(null)}
          />
        )}

        {/* Settings panel */}
        {settingsOpen && !isDraft && (
          <SettingsPanel
            autonomy={autonomy}
            onChangeAutonomy={changeAutonomy}
            onClose={() => setSettingsOpen(false)}
          />
        )}

        {/* Versions panel */}
        {versionsOpen && !isDraft && props.name && (
          <VersionsPanel
            name={props.name}
            currentVersion={meta?.version ?? 0}
            onClose={() => setVersionsOpen(false)}
            onPromoted={() => {
              setVersionsOpen(false)
              setLoading(true)
              playbooksApi.get(props.name!).then((pb) => {
                const def = pb.definition as PlaybookDef
                setDefinition(def)
                setMeta({
                  display_name: pb.display_name,
                  status: pb.status as string,
                  version: pb.version,
                  isDraft: false,
                })
                setYamlText(JSON.stringify(def, null, 2))
                const { nodes: n, edges: e } = buildGraph(def)
                setNodes(n)
                setEdges(e)
              }).catch((e) => setError(e.message)).finally(() => {
                setLoading(false)
                setVersionsOpen(true)
              })
            }}
          />
        )}
      </div>
    </div>
  )
}

const AUTONOMY_OPTIONS: {
  value: string
  label: string
  description: string
  icon: React.ComponentType<{ className?: string }>
  color: string
}[] = [
  {
    value: 'agent_may_trigger',
    label: 'Always allowed',
    description: 'Agent can run this playbook anytime without asking. Best for trusted, low-risk playbooks.',
    icon: ShieldCheck,
    color: 'text-emerald-400',
  },
  {
    value: 'agent_must_confirm',
    label: 'Ask first',
    description: 'Agent must ask you before the first run. Once you approve, it becomes always allowed.',
    icon: ShieldAlert,
    color: 'text-amber-400',
  },
  {
    value: 'manual_only',
    label: 'Never',
    description: 'Agent cannot run this playbook at all. Only you can trigger it manually via the API.',
    icon: ShieldOff,
    color: 'text-rose-400',
  },
]

function SettingsPanel({
  autonomy,
  onChangeAutonomy,
  onClose,
}: {
  autonomy: string
  onChangeAutonomy: (value: string) => void
  onClose: () => void
}) {
  return (
    <div className="w-[320px] shrink-0 border-l border-white/5 bg-ink-950/80 backdrop-blur-sm overflow-y-auto">
      <div className="flex items-center justify-between px-4 py-3 border-b border-white/5">
        <div className="flex items-center gap-2">
          <Settings className="w-4 h-4 text-ink-400" />
          <span className="text-sm font-medium text-ink-100">Settings</span>
        </div>
        <button
          onClick={onClose}
          className="p-1 rounded hover:bg-white/10 text-ink-500 hover:text-ink-200 transition"
        >
          <X className="w-3.5 h-3.5" />
        </button>
      </div>

      <div className="px-4 py-4">
        <div className="text-[11px] uppercase tracking-wider text-ink-500 mb-3">
          Agent can trigger
        </div>
        <div className="space-y-1.5">
          {AUTONOMY_OPTIONS.map((opt) => {
            const Icon = opt.icon
            const selected = autonomy === opt.value
            return (
              <button
                key={opt.value}
                onClick={() => onChangeAutonomy(opt.value)}
                className={cn(
                  'w-full text-left rounded-lg px-3 py-2.5 transition border',
                  selected
                    ? 'border-white/10 bg-white/[.04]'
                    : 'border-transparent hover:bg-white/[.02]',
                )}
              >
                <div className="flex items-center gap-2">
                  <Icon className={cn('w-4 h-4 shrink-0', opt.color)} />
                  <span className={cn(
                    'text-sm font-medium',
                    selected ? 'text-ink-100' : 'text-ink-300',
                  )}>
                    {opt.label}
                  </span>
                  {selected && <Check className="w-3.5 h-3.5 text-luna-400 ml-auto shrink-0" />}
                </div>
                <p className="text-[11px] text-ink-500 mt-1 ml-6 leading-relaxed">
                  {opt.description}
                </p>
              </button>
            )
          })}
        </div>
      </div>
    </div>
  )
}

function timeAgo(iso: string): string {
  const seconds = Math.floor((Date.now() - new Date(iso).getTime()) / 1000)
  if (seconds < 60) return 'just now'
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  if (days < 7) return `${days}d ago`
  return new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
}

type VersionEntry = {
  version: number
  title: string
  author: string
  created_at: string
  runs: number
  promoted_from: number | null
  current: boolean
}

function VersionsPanel({
  name,
  currentVersion,
  onClose,
  onPromoted,
}: {
  name: string
  currentVersion: number
  onClose: () => void
  onPromoted: () => void
}) {
  const [versions, setVersions] = useState<VersionEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [promoting, setPromoting] = useState<number | null>(null)

  useEffect(() => {
    playbooksApi.listVersions(name)
      .then(setVersions)
      .finally(() => setLoading(false))
  }, [name, currentVersion])

  const handlePromote = async (version: number) => {
    setPromoting(version)
    try {
      await playbooksApi.promoteVersion(name, version)
      onPromoted()
    } catch {
      setPromoting(null)
    }
  }

  return (
    <div className="w-[320px] shrink-0 border-l border-white/5 bg-ink-950/80 backdrop-blur-sm overflow-y-auto">
      <div className="flex items-center justify-between px-4 py-3 border-b border-white/5">
        <div className="flex items-center gap-2">
          <History className="w-4 h-4 text-ink-400" />
          <span className="text-sm font-medium text-ink-100">Versions</span>
        </div>
        <button
          onClick={onClose}
          className="p-1 rounded hover:bg-white/10 text-ink-500 hover:text-ink-200 transition"
        >
          <X className="w-3.5 h-3.5" />
        </button>
      </div>

      <div className="px-2 py-2">
        {loading ? (
          <div className="flex items-center justify-center py-8 text-ink-500">
            <Loader2 className="w-4 h-4 animate-spin" />
          </div>
        ) : versions.length === 0 ? (
          <p className="text-xs text-ink-500 text-center py-8">No version history yet</p>
        ) : (
          <div className="space-y-1">
            {versions.map((v) => (
              <div
                key={v.version}
                className="rounded-lg px-3 py-2.5 border border-white/5 bg-white/[.02]"
              >
                <div className="flex items-center gap-2">
                  <span className="text-xs font-semibold text-ink-200">v{v.version}</span>
                  {v.current && (
                    <span className="px-1.5 py-0.5 rounded text-[10px] font-medium bg-luna-600/30 text-luna-300">
                      current
                    </span>
                  )}
                </div>
                {v.title && (
                  <p className="text-xs text-ink-400 mt-1 truncate" title={v.title}>
                    "{v.title}"
                  </p>
                )}
                <div className="flex items-center gap-2 mt-1.5 text-[11px] text-ink-500">
                  <span>{v.author === 'agent' ? 'agent' : v.author === 'owner' ? 'you' : v.author || '—'}</span>
                  <span>·</span>
                  <span>{timeAgo(v.created_at)}</span>
                  <span>·</span>
                  <span>{v.runs} {v.runs === 1 ? 'run' : 'runs'}</span>
                </div>
                {v.promoted_from != null && (
                  <p className="text-[10px] text-ink-600 mt-1">
                    ← promoted from v{v.promoted_from}
                  </p>
                )}
                {!v.current && (
                  <button
                    onClick={() => handlePromote(v.version)}
                    disabled={promoting !== null}
                    className="mt-2 inline-flex items-center gap-1 px-2 py-1 rounded text-[11px] font-medium text-luna-400 hover:bg-luna-600/20 transition disabled:opacity-40"
                  >
                    {promoting === v.version ? (
                      <Loader2 className="w-3 h-3 animate-spin" />
                    ) : (
                      <ArrowUpCircle className="w-3 h-3" />
                    )}
                    Promote
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

function fmtDuration(start: string | null, end: string | null): string | null {
  if (!start || !end) return null
  const ms = new Date(end).getTime() - new Date(start).getTime()
  if (ms < 0) return null
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(2)}s`
}

function StepDetailPanel({
  step, execRows, hasRun, onClose,
}: {
  step: StepDef
  execRows: StepRunDetail[]
  hasRun: boolean
  onClose: () => void
}) {
  const kind = step.kind as StepKind
  const colors = STEP_COLORS[kind] || STEP_COLORS.tool_call
  const Icon = KIND_ICONS[kind] || Zap
  const rows = stepDetailRows(step)
  // Loop bodies run once per iteration → many rows. Show the last execution and
  // note the count.
  const lastExec = execRows[execRows.length - 1] || null
  // 006.714: raw JSON is hidden by default — humans read the one-line summary.
  const [showRaw, setShowRaw] = useState(false)

  return (
    <div className="w-[320px] shrink-0 border-l border-white/5 bg-ink-950/80 backdrop-blur-sm overflow-y-auto">
      <div className="flex items-center justify-between px-4 py-3 border-b border-white/5">
        <div className="flex items-center gap-2 min-w-0">
          <div className={cn(
            'w-6 h-6 rounded-md flex items-center justify-center shrink-0',
            kind === 'agent_step' ? 'bg-indigo-800/60' :
            kind === 'tool_call' ? 'bg-teal-800/60' :
            kind === 'condition' ? 'bg-amber-800/60' :
            kind === 'wait_for_approval' || kind === 'wait_for_event' ? 'bg-orange-800/60' :
            kind === 'loop' ? 'bg-purple-800/60' :
            kind === 'state' ? 'bg-emerald-800/60' :
            kind === 'halt' ? 'bg-rose-800/60' :
            'bg-ink-800/60'
          )}>
            <Icon className={cn('w-3.5 h-3.5', colors.text)} />
          </div>
          <div className="min-w-0">
            <div className={cn('text-xs font-semibold truncate', colors.text)}>
              {step.id}
            </div>
            <div className="text-[10px] text-ink-500 capitalize">{kind.replace(/_/g, ' ')}</div>
          </div>
        </div>
        <button
          onClick={onClose}
          className="p-1 rounded hover:bg-white/10 text-ink-500 hover:text-ink-200 transition"
        >
          <X className="w-3.5 h-3.5" />
        </button>
      </div>

      <div className="px-4 py-3 space-y-3">
        {step.explanation && (
          <div>
            <div className="text-[10px] uppercase tracking-wider text-ink-600 mb-1">Explanation</div>
            <p className="text-xs text-ink-300 leading-relaxed">{step.explanation}</p>
          </div>
        )}

        {rows.length > 0 && (
          <div className="space-y-2">
            <div className="text-[10px] uppercase tracking-wider text-ink-600">Configuration</div>
            {rows.map((r) => (
              <div key={r.label}>
                <div className="text-[10px] text-ink-500 mb-0.5">{r.label}</div>
                <div className={cn(
                  'text-xs text-ink-300',
                  r.value.includes('\n') ? 'font-mono whitespace-pre-wrap text-[10px] bg-ink-900/60 rounded p-2' : '',
                )}>
                  {r.value}
                </div>
              </div>
            ))}
          </div>
        )}

        {!step.explanation && rows.length === 0 && (
          <p className="text-xs text-ink-600 italic">No static config for this step.</p>
        )}

        {/* 007.009.01: execution detail for the selected run */}
        {hasRun && (
          <div className="space-y-2 pt-2 border-t border-white/5">
            <div className="flex items-center gap-2">
              <div className="text-[10px] uppercase tracking-wider text-ink-600">Execution</div>
              {execRows.length > 1 && (
                <span className="text-[9px] text-ink-500 px-1 py-0.5 rounded bg-ink-800">
                  {execRows.length} runs
                </span>
              )}
            </div>

            {!lastExec ? (
              <p className="text-xs text-ink-600 italic">Did not run in the selected run.</p>
            ) : (
              <>
                <div className="flex items-center gap-2 flex-wrap text-[10px]">
                  <span className={cn(
                    'px-1.5 py-0.5 rounded font-medium',
                    lastExec.status === 'failed' ? 'bg-rose-900/40 text-rose-300' :
                    lastExec.status === 'running' ? 'bg-blue-900/40 text-blue-300' :
                    lastExec.status === 'waiting' ? 'bg-amber-900/40 text-amber-300' :
                    'bg-emerald-900/40 text-emerald-300',
                  )}>
                    {lastExec.status}
                  </span>
                  {fmtDuration(lastExec.started_at, lastExec.completed_at) && (
                    <span className="text-ink-500">{fmtDuration(lastExec.started_at, lastExec.completed_at)}</span>
                  )}
                  {lastExec.cost_cents != null && lastExec.cost_cents > 0 && (
                    <span className="text-ink-500">${(lastExec.cost_cents / 100).toFixed(4)}</span>
                  )}
                  {lastExec.retry_count != null && lastExec.retry_count > 0 && (
                    <span className="text-amber-400">{lastExec.retry_count} retr{lastExec.retry_count === 1 ? 'y' : 'ies'}</span>
                  )}
                  {kind === 'loop' && lastExec.outputs?.iterations != null && (
                    <span className="text-purple-300">{String(lastExec.outputs.iterations)} iterations</span>
                  )}
                  {kind === 'loop' && lastExec.outputs?.stopped && (
                    <span className="text-amber-400">stopped: {String(lastExec.outputs.stopped)}</span>
                  )}
                </div>

                {/* 006.714: plain-English summary first — humans before JSON. */}
                <p className="text-xs text-ink-300 leading-relaxed">
                  {execSummary(step, lastExec)}
                </p>

                {lastExec.error && (
                  <div>
                    <div className="text-[10px] text-ink-500 mb-0.5">Error</div>
                    <pre className="text-[10px] text-rose-300 font-mono whitespace-pre-wrap bg-rose-950/30 rounded p-2 max-h-40 overflow-auto">
                      {lastExec.error}
                    </pre>
                  </div>
                )}

                {((lastExec.inputs && Object.keys(lastExec.inputs).length > 0) || lastExec.outputs != null) && (
                  <div>
                    <button
                      onClick={() => setShowRaw((v) => !v)}
                      className="flex items-center gap-1 text-[10px] text-ink-500 hover:text-ink-300 transition"
                    >
                      {showRaw ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                      {showRaw ? 'Hide raw data' : 'Show raw input / output'}
                    </button>
                    {showRaw && (
                      <div className="mt-2 space-y-2">
                        {lastExec.inputs && Object.keys(lastExec.inputs).length > 0 && (
                          <div>
                            <div className="text-[10px] text-ink-500 mb-0.5">Resolved inputs</div>
                            <pre className="text-[10px] text-ink-300 font-mono whitespace-pre-wrap bg-ink-900/60 rounded p-2 max-h-48 overflow-auto">
                              {JSON.stringify(lastExec.inputs, null, 2)}
                            </pre>
                          </div>
                        )}
                        {lastExec.outputs != null && (
                          <div>
                            <div className="text-[10px] text-ink-500 mb-0.5">Output</div>
                            <pre className="text-[10px] text-ink-300 font-mono whitespace-pre-wrap bg-ink-900/60 rounded p-2 max-h-56 overflow-auto">
                              {JSON.stringify(lastExec.outputs, null, 2)}
                            </pre>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </>
            )}
          </div>
        )}
      </div>
    </div>
  )
}

// 006.714: the replay control makes it explicit that the canvas is showing a
// PAST run — it names when the run happened ("from 3h ago") and its trigger, so
// the animation is never mistaken for a live execution. A clear (×) returns the
// canvas to the clean definition.
function ReplayToggle({
  playing,
  startedAt,
  trigger,
  onToggle,
  onClear,
}: {
  playing: boolean
  startedAt: string | null
  trigger: string | null
  onToggle: () => void
  onClear: () => void
}) {
  const when = fmtRelativeTime(startedAt)
  const src = trigger && trigger !== 'manual' ? ` · ${trigger}` : ''
  return (
    <div className="absolute top-3 left-1/2 -translate-x-1/2 z-20 flex items-center gap-1.5 px-2.5 py-1.5 rounded-full border border-amber-500/30 bg-ink-950/90 backdrop-blur-sm shadow-lg text-xs text-amber-100">
      <span className="text-[10px] uppercase tracking-wide text-amber-400/80 font-medium">Past run</span>
      <button
        data-testid="replay-play"
        onClick={onToggle}
        title={playing ? 'Stop replay' : `Replay run from ${when}`}
        className="flex items-center gap-1.5 pl-1 text-ink-100 transition hover:text-white"
      >
        {playing ? <Square className="w-3.5 h-3.5" /> : <Play className="w-3.5 h-3.5" />}
        {playing ? 'Stop' : 'Replay'} · {when}{src}
      </button>
      <button
        onClick={onClear}
        title="Back to definition"
        className="p-0.5 rounded text-ink-500 hover:text-ink-200 transition"
      >
        <X className="w-3.5 h-3.5" />
      </button>
    </div>
  )
}

// 006.714: relative time for the replay banner ("3h ago"). Shared shape with
// PlaybookRuns' fmtRelative (kept local to avoid a cross-file util import).
function fmtRelativeTime(iso: string | null): string {
  if (!iso) return 'unknown time'
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return 'unknown time'
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000))
  if (s < 45) return 'just now'
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  const d = Math.floor(h / 24)
  if (d < 30) return `${d}d ago`
  const mo = Math.floor(d / 30)
  return mo < 12 ? `${mo}mo ago` : `${Math.floor(mo / 12)}y ago`
}

function TabBtn({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        'flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium transition',
        active
          ? 'bg-luna-600/30 text-luna-200'
          : 'text-ink-400 hover:text-ink-200 hover:bg-white/5',
      )}
    >
      {children}
    </button>
  )
}
