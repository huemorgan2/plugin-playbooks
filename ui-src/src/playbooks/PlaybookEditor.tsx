import { useCallback, useEffect, useRef, useState } from 'react'
import {
  useNodesState,
  useEdgesState,
  type Node,
  type Edge,
  type NodeMouseHandler,
} from '@xyflow/react'
import {
  ArrowLeft, Play, FileCode, Eye, Loader2, Rocket,
  X, ChevronDown, ChevronRight, Settings, History,
  ShieldCheck, ShieldAlert, ShieldOff, Check, ArrowUpCircle,
  FileText, FlaskConical,
} from 'lucide-react'
import { cn } from '../lib/cn'
import { buildGraph } from './layout'
import { CanvasSurface, CodeView, sourceFor } from './VersionCanvas'
import { applyPlaybookPatch, patchMatchesEditor, type PlaybookPatchEvt } from './livePatch'
import { setPlaybookConsumerReady } from './liveBus'
import { subscribePlaybookEvents } from '../lib/events'
import { playbooksApi } from './api'
import { useAgentName } from './agentIdentity'
import type {
  PlaybookDef, StepDef, StepKind, StepRunDetail,
  PlaybookRunDetail,
} from './types'
import { STEP_COLORS } from './types'
import { RunsTab } from './RunsTab'
import { TestsTab } from './TestsTab'
import { ManifestTab } from './ManifestTab'

import { KIND_LABELS, kindIcon } from './explain/primitives'
import { IntegrationIcon, toolIconUrl, useIconRef } from './icons'
import { headline } from './explain/headline'
import { STEP_EXPLAINERS, DataFlow, FooterChips } from './explain/registry'
import { findStepById } from './explain/dataflow'
import { JsonTree } from './explain/jsontree'

// 0.13.0 (plans/002 phase 6): view modes became real tabs. Drafts keep
// Canvas | Code only — the other tabs are live-playbook surfaces.
type ViewMode = 'canvas' | 'code' | 'manifest' | 'tests' | 'runs'

type Props =
  | { name: string; draftId?: undefined; onBack: () => void }
  | { name?: undefined; draftId: string; onBack: () => void }

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

// The promote REST path refuses with a 422 whose body names the failing gate.
// apiFetch surfaces it as "422: {json}" — dig the human message out.
export function promoteRefusalMessage(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err)
  const jsonStart = raw.indexOf('{')
  if (jsonStart >= 0) {
    try {
      const body = JSON.parse(raw.slice(jsonStart))
      const detail = body.detail ?? body
      if (detail?.message) return detail.message
      if (detail?.gate) return `Promote refused — gate '${detail.gate}' failed`
    } catch { /* fall through */ }
  }
  return raw
}

export function PlaybookEditor(props: Props) {
  const agentName = useAgentName()
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
  // 0.13.0: live pblang source + candidate content for the canvas switch.
  const [code, setCode] = useState<string | null>(null)
  const [candidateVersion, setCandidateVersion] = useState<number | null>(null)
  const [candidateDef, setCandidateDef] = useState<PlaybookDef | null>(null)
  const [candidateCode, setCandidateCode] = useState<string | null>(null)
  const [canvasSource, setCanvasSource] = useState<'live' | 'candidate'>('live')
  const [promoteError, setPromoteError] = useState<string | null>(null)
  const [viewMode, setViewMode] = useState<ViewMode>('canvas')
  const [promoting, setPromoting] = useState(false)
  const [selectedStep, setSelectedStep] = useState<StepDef | null>(null)
  const [autonomy, setAutonomy] = useState<string>('agent_must_confirm')
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [versionsOpen, setVersionsOpen] = useState(false)

  // A selected run colors the canvas by step status (no animation, 0.13.0).
  const [runDetail, setRunDetail] = useState<PlaybookRunDetail | null>(null)

  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([])
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([])

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
            version: pb.live_version ?? pb.version,
            isDraft: false,
          })
          setCode(pb.code ?? null)
          setCandidateVersion(pb.candidate_version ?? null)
          setCandidateDef((pb.candidate_definition as PlaybookDef) ?? null)
          setCandidateCode(pb.candidate_code ?? null)
          setCanvasSource('live')
          const { nodes: n, edges: e } = buildGraph(def)
          setNodes(n)
          setEdges(e)
        })

    load.catch((e) => setError(e.message)).finally(() => setLoading(false))
  }, [props.draftId, props.name, setNodes, setEdges])

  useEffect(() => { loadData() }, [loadData])

  // Rebuild the canvas from the chosen source (live definition, the candidate,
  // or live + a selected run's statuses).
  const showSource = useCallback((source: 'live' | 'candidate') => {
    setCanvasSource(source)
    setSelectedStep(null)
    const def = source === 'candidate' ? candidateDef : defRef.current
    if (!def) return
    if (source === 'candidate') setRunDetail(null)
    const { nodes: n, edges: e } = buildGraph(def)
    setNodes(n)
    setEdges(e)
  }, [candidateDef, setNodes, setEdges])

  // Load a run's detail and color the graph by step status (live def only).
  const loadCanvasRun = useCallback(async (runId: string) => {
    try {
      const detail = await playbooksApi.getRun(runId)
      const def = defRef.current
      setRunDetail(detail)
      setCanvasSource('live')
      if (def) {
        const { nodes: n, edges: e } = buildGraph(def, detail.steps)
        setNodes(n)
        setEdges(e)
      }
    } catch { /* ignore */ }
  }, [setNodes, setEdges])

  // "Show on canvas" from the Runs tab — switch to the canvas, color by status.
  const loadRunStatic = useCallback((runId: string) => {
    setViewMode('canvas')
    loadCanvasRun(runId)
  }, [loadCanvasRun])

  // Refresh a loaded run's statuses without a layout rebuild. Shared by the
  // fallback poll and the live playbook.step.* nudges.
  const refreshRunStatuses = useCallback(async (runId: string) => {
    try {
      const fresh = await playbooksApi.getRun(runId)
      setRunDetail(fresh)
      const byStep = new Map(fresh.steps.map((s) => [s.step_id, s.status]))
      setNodes((prev) => prev.map((n) => {
        const sid = (n.data as any).stepId as string | undefined
        const st = sid ? byStep.get(sid) : undefined
        return st ? { ...n, data: { ...n.data, runStatus: st } } : n
      }))
    } catch { /* ignore */ }
  }, [setNodes])

  // Live attach: a run of THIS playbook started elsewhere (chat tool, trigger,
  // cron) — load it onto the canvas so the run is visible without the owner
  // digging through the Runs tab. Step/completion events nudge an immediate
  // refresh so movement is instant; the poll below covers anything missed.
  const runDetailRef = useRef<PlaybookRunDetail | null>(null)
  useEffect(() => { runDetailRef.current = runDetail }, [runDetail])
  useEffect(() => {
    return subscribePlaybookEvents((evt) => {
      const current = runDetailRef.current
      if (evt.event === 'playbook.run.started') {
        // attach only when idle — never yank the canvas off a run the owner
        // is already watching
        if (props.name && evt.playbook_name === props.name
            && (!current || current.status !== 'running')) {
          setViewMode('canvas')
          loadCanvasRun(evt.run_id)
        }
        return
      }
      if (current && evt.run_id === current.id) {
        void refreshRunStatuses(evt.run_id)
      }
    })
  }, [props.name, loadCanvasRun, refreshRunStatuses])

  // Near-live: poll a running run as a fallback for missed events.
  useEffect(() => {
    if (!runDetail || runDetail.status !== 'running') return
    const t = setTimeout(() => { void refreshRunStatuses(runDetail.id) }, 1400)
    return () => clearTimeout(t)
  }, [runDetail, refreshRunStatuses])

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
    // The agent is editing the playbook — drop any run coloring so build-glow
    // and status colors don't fight over the same nodes.
    setRunDetail(null)
    setCanvasSource('live')
    const { def: nextDef, glowNodeId } = applyPlaybookPatch(cur, evt)
    defRef.current = nextDef
    setDefinition(nextDef)
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

  const handlePromoteDraft = async () => {
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

  // 0.13.0: promote the pending candidate through the gates (static
  // validation, tests, tool probes). A refusal names the failing gate inline.
  const handlePromoteCandidate = async () => {
    if (!props.name || promoting) return
    setPromoting(true)
    setPromoteError(null)
    try {
      await playbooksApi.promoteCandidate(props.name)
      setLoading(true)
      loadData()
    } catch (e: any) {
      setPromoteError(promoteRefusalMessage(e))
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
  const canvasDef = canvasSource === 'candidate' && candidateDef ? candidateDef : definition
  const playbookExplanation = canvasDef?.explanation
  const codeShown = canvasSource === 'candidate' && candidateDef
    ? sourceFor(candidateCode, candidateDef)
    : sourceFor(code, definition)

  const tabs: { mode: ViewMode; label: string; icon: React.ComponentType<{ className?: string }> }[] =
    isDraft
      ? [
          { mode: 'canvas', label: 'Canvas', icon: Eye },
          { mode: 'code', label: 'Code', icon: FileCode },
        ]
      : [
          { mode: 'canvas', label: 'Canvas', icon: Eye },
          { mode: 'code', label: 'Code', icon: FileCode },
          { mode: 'manifest', label: 'Manifest', icon: FileText },
          { mode: 'tests', label: 'Tests', icon: FlaskConical },
          { mode: 'runs', label: 'Runs', icon: Play },
        ]

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
          {!isDraft && candidateVersion != null && (
            <button
              onClick={handlePromoteCandidate}
              disabled={promoting}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-violet-500/40 text-violet-300 hover:bg-violet-600/20 disabled:opacity-40 text-xs font-medium transition whitespace-nowrap shrink-0"
              title="Run the gates (tests, tool checks) and make this candidate live"
              data-testid="promote-candidate-btn"
            >
              {promoting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Rocket className="w-3.5 h-3.5" />}
              Promote candidate v{candidateVersion}
            </button>
          )}
          {!isDraft && (
            <>
              <button
                onClick={() => { setVersionsOpen(!versionsOpen); setSettingsOpen(false) }}
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
                onClick={() => { setSettingsOpen(!settingsOpen); setVersionsOpen(false) }}
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
              onClick={handlePromoteDraft}
              disabled={promoting || !hasSteps}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 text-white text-xs font-medium transition"
              title={!hasSteps ? 'Add steps before promoting' : 'Promote to live playbook'}
            >
              {promoting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Rocket className="w-3.5 h-3.5" />}
              Promote
            </button>
          )}
          <div className="flex items-center gap-1 bg-ink-900/60 rounded-lg p-0.5">
            {tabs.map(({ mode, label, icon: Icon }) => (
              <TabBtn key={mode} active={viewMode === mode} onClick={() => setViewMode(mode)}>
                <Icon className="w-3.5 h-3.5" /> {label}
              </TabBtn>
            ))}
          </div>
        </div>
      </div>

      {/* A refused candidate promote names the failing gate right under the header. */}
      {promoteError && (
        <div className="flex items-center gap-2 px-4 py-2 border-b border-rose-500/20 bg-rose-950/20 shrink-0">
          <p className="text-xs text-rose-300 flex-1" data-testid="promote-error">{promoteError}</p>
          <button
            onClick={() => setPromoteError(null)}
            className="p-0.5 rounded text-ink-500 hover:text-ink-200 transition"
          >
            <X className="w-3.5 h-3.5" />
          </button>
        </div>
      )}

      {/* Content */}
      <div className="flex-1 min-h-0 relative flex">
        <div className="flex-1 min-w-0 relative">
          {viewMode === 'canvas' && (
            <CanvasSurface
              name={canvasDef?.name || props.name || ''}
              explanation={playbookExplanation}
              agentName={agentName}
              hasSteps={hasSteps || !!(canvasSource === 'candidate' && candidateDef?.steps?.length)}
              nodes={nodes}
              edges={edges}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onNodeClick={handleNodeClick}
              onPaneClick={handlePaneClick}
              runDetail={!isDraft && canvasSource === 'live' ? runDetail : null}
              onClearRun={() => {
                setRunDetail(null)
                if (defRef.current) {
                  const { nodes: n, edges: e } = buildGraph(defRef.current)
                  setNodes(n)
                  setEdges(e)
                }
              }}
              overlay={
                /* 0.13.0: live/candidate switch — which version the canvas shows. */
                !isDraft && candidateVersion != null && candidateDef ? (
                  <div className="absolute top-3 left-1/2 -translate-x-1/2 z-20 flex items-center gap-0.5 p-0.5 rounded-full border border-white/10 bg-ink-950/90 backdrop-blur-sm shadow-lg">
                    <button
                      onClick={() => showSource('live')}
                      className={cn(
                        'px-2.5 py-1 rounded-full text-[11px] font-medium transition',
                        canvasSource === 'live'
                          ? 'bg-luna-600/30 text-luna-200'
                          : 'text-ink-400 hover:text-ink-200',
                      )}
                      data-testid="canvas-source-live"
                    >
                      Live v{meta?.version}
                    </button>
                    <button
                      onClick={() => showSource('candidate')}
                      className={cn(
                        'px-2.5 py-1 rounded-full text-[11px] font-medium transition',
                        canvasSource === 'candidate'
                          ? 'bg-violet-600/30 text-violet-200'
                          : 'text-ink-400 hover:text-ink-200',
                      )}
                      data-testid="canvas-source-candidate"
                    >
                      Candidate v{candidateVersion}
                    </button>
                  </div>
                ) : null
              }
            />
          )}

          {viewMode === 'code' && (
            <CodeView
              source={codeShown}
              agentName={agentName}
              header={
                !isDraft && candidateVersion != null && candidateDef ? (
                  <div className="flex items-center gap-1 mb-3">
                    {(['live', 'candidate'] as const).map((src) => (
                      <button
                        key={src}
                        onClick={() => showSource(src)}
                        className={cn(
                          'px-2.5 py-1 rounded-md text-[11px] font-medium transition',
                          canvasSource === src
                            ? src === 'candidate' ? 'bg-violet-600/30 text-violet-200' : 'bg-luna-600/30 text-luna-200'
                            : 'text-ink-400 hover:text-ink-200 hover:bg-white/5',
                        )}
                      >
                        {src === 'live' ? `Live v${meta?.version}` : `Candidate v${candidateVersion}`}
                      </button>
                    ))}
                  </div>
                ) : null
              }
            />
          )}

          {viewMode === 'manifest' && !isDraft && props.name && (
            <ManifestTab
              name={props.name}
              onSaved={(version) => setMeta((m) => (m ? { ...m, version } : m))}
            />
          )}

          {viewMode === 'tests' && !isDraft && props.name && (
            <TestsTab name={props.name} />
          )}

          {viewMode === 'runs' && !isDraft && props.name && (
            <RunsTab name={props.name} onShowOnCanvas={loadRunStatic} />
          )}
        </div>

        {/* Step detail panel */}
        {selectedStep && viewMode === 'canvas' && !settingsOpen && (
          <StepDetailPanel
            step={selectedStep}
            execRows={execRowsForStep(runDetail, selectedStep.id)}
            hasRun={!!runDetail}
            onClose={() => setSelectedStep(null)}
            onSelectStep={setSelectedStep}
            onJumpToStep={(id) => {
              const hit = findStepById(canvasDef?.steps || [], id)
              if (hit) setSelectedStep(hit)
            }}
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
              loadData()
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
  // 0.27.1 (plans/016 phase 1): a refused restore used to be swallowed —
  // the owner clicked Promote and nothing happened. Show the gate's reason.
  const [promoteError, setPromoteError] = useState<string | null>(null)

  useEffect(() => {
    playbooksApi.listVersions(name)
      .then(setVersions)
      .finally(() => setLoading(false))
  }, [name, currentVersion])

  const handlePromote = async (version: number) => {
    setPromoting(version)
    setPromoteError(null)
    try {
      await playbooksApi.promoteVersion(name, version)
      onPromoted()
    } catch (e) {
      setPromoteError(promoteRefusalMessage(e))
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

      {promoteError && (
        <div
          className="flex items-start gap-2 px-4 py-2 border-b border-rose-500/20 bg-rose-950/30"
          data-testid="versions-promote-error"
        >
          <p className="text-xs text-rose-300 flex-1">{promoteError}</p>
          <button
            onClick={() => setPromoteError(null)}
            className="p-0.5 rounded hover:bg-white/10 text-rose-400/70 hover:text-rose-200 transition"
            title="Dismiss"
          >
            <X className="w-3 h-3" />
          </button>
        </div>
      )}

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
  step, execRows, hasRun, onClose, onSelectStep, onJumpToStep,
}: {
  step: StepDef
  execRows: StepRunDetail[]
  hasRun: boolean
  onClose: () => void
  onSelectStep: (s: StepDef) => void
  onJumpToStep: (id: string) => void
}) {
  const kind = step.kind as StepKind
  const colors = STEP_COLORS[kind] || STEP_COLORS.tool_call
  const Icon = kindIcon(kind)
  // plans/011: a tool step's header wears the integration icon.
  const iconRef = useIconRef()
  const toolUrl = kind === 'tool_call' ? toolIconUrl(iconRef, step.tool) : null
  // plans/010: per-kind explain renderer, 100% derived from the definition.
  const Explainer = STEP_EXPLAINERS[kind] || STEP_EXPLAINERS.tool_call
  // Loop bodies run once per iteration → many rows. Show the last execution and
  // note the count.
  const lastExec = execRows[execRows.length - 1] || null
  // 006.714: raw JSON is hidden by default — humans read the one-line summary.
  const [showRaw, setShowRaw] = useState(false)
  const [showRawDef, setShowRawDef] = useState(false)

  return (
    <div className="w-[420px] shrink-0 border-l border-white/5 bg-ink-950/80 backdrop-blur-sm overflow-y-auto">
      {/* Eyebrow row: kind + id. The bottom line lives right below as the headline. */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-white/5">
        <div className="flex items-center gap-2 min-w-0">
          <div className={cn(
            'w-6 h-6 rounded-md flex items-center justify-center shrink-0',
            kind === 'agent_step' ? 'bg-indigo-800/60' :
            kind === 'llm_step' ? 'bg-fuchsia-800/60' :
            kind === 'tool_call' ? 'bg-teal-800/60' :
            kind === 'condition' ? 'bg-amber-800/60' :
            kind === 'wait_for_approval' || kind === 'wait_for_event' ? 'bg-orange-800/60' :
            kind === 'subtask' ? 'bg-violet-800/60' :
            kind === 'loop' ? 'bg-purple-800/60' :
            kind === 'state' ? 'bg-emerald-800/60' :
            kind === 'code' ? 'bg-cyan-800/60' :
            kind === 'halt' ? 'bg-rose-800/60' :
            'bg-ink-800/60'
          )}>
            <IntegrationIcon
              url={toolUrl}
              fallback={Icon}
              fallbackClass={cn('w-3.5 h-3.5', colors.text)}
              className="w-6 h-6 rounded-md"
            />
          </div>
          <span className={cn('text-[10px] uppercase tracking-[0.16em] font-semibold shrink-0', colors.text)}>
            {KIND_LABELS[kind] || kind}
          </span>
          <span className="text-[10px] font-mono text-ink-500 truncate">{step.id}</span>
        </div>
        <button
          onClick={onClose}
          className="p-1 rounded hover:bg-white/10 text-ink-500 hover:text-ink-200 transition"
        >
          <X className="w-3.5 h-3.5" />
        </button>
      </div>

      <div className="px-4 py-3 space-y-3">
        {/* Bottom line first — generated from the definition, never from prose. */}
        <div className="text-sm font-semibold text-ink-100 leading-snug" data-testid="step-headline">
          {headline(step)}
        </div>

        {step.explanation && (
          <p className="text-xs text-ink-400 leading-relaxed border-l-2 border-white/10 pl-2.5">
            {step.explanation}
          </p>
        )}

        <Explainer step={step} onSelectStep={onSelectStep} />

        <FooterChips step={step} />

        <DataFlow step={step} onJumpToStep={onJumpToStep} />

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
                            <JsonTree data={lastExec.inputs} />
                          </div>
                        )}
                        {lastExec.outputs != null && (
                          <div>
                            <div className="text-[10px] text-ink-500 mb-0.5">Output</div>
                            <JsonTree data={lastExec.outputs} />
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

        {/* plans/010: the escape hatch — the exact stored definition, collapsed. */}
        <div className="pt-2 border-t border-white/5">
          <button
            onClick={() => setShowRawDef((v) => !v)}
            className="flex items-center gap-1 text-[10px] text-ink-500 hover:text-ink-300 transition"
            data-testid="raw-def-toggle"
          >
            {showRawDef ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
            Raw definition
          </button>
          {showRawDef && (
            <pre className="mt-2 text-[10px] text-ink-300 font-mono whitespace-pre-wrap bg-ink-900/60 rounded p-2 max-h-64 overflow-auto">
              {JSON.stringify(step, null, 2)}
            </pre>
          )}
        </div>
      </div>
    </div>
  )
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
