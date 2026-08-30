import { useCallback, useEffect, useRef, useState } from 'react'
import {
  useNodesState,
  useEdgesState,
  type Node,
  type Edge,
  type NodeMouseHandler,
} from '@xyflow/react'
import {
  ArrowLeft, FileCode, Eye, Loader2, Rocket, Settings, History,
  ShieldCheck, ShieldAlert, ShieldOff, Check,
} from 'lucide-react'
import { cn } from '../lib/cn'
import { buildGraph } from './layout'
import { CanvasSurface, CodeView, sourceFor } from './VersionCanvas'
import { applyPlaybookPatch, patchMatchesEditor, type PlaybookPatchEvt } from './livePatch'
import { setPlaybookConsumerReady } from './liveBus'
import { playbooksApi } from './api'
import { useAgentName } from './agentIdentity'
import type { PlaybookDef, StepDef } from './types'
import { StepDetailPanel } from './StepDetailPanel'
import { TabBtn } from './editorBits'
import { VersionsTab } from './VersionsTab'
import { findStepById } from './explain/dataflow'

export { promoteRefusalMessage } from './VersionsTab'

// plans/016 phase 4: a playbook is its versions. Everything that used to be a
// top-level tab (Canvas, Code, Manifest, Tests, Runs) is a view of the
// selected version inside `Versions`. Drafts have no history and keep
// Canvas | Code.
type PlaybookMode = 'versions' | 'settings'
type DraftMode = 'canvas' | 'code'

type Props =
  | { name: string; draftId?: undefined; onBack: () => void }
  | { name?: undefined; draftId: string; onBack: () => void }

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
    candidateVersion: number | null
    isDraft: boolean
    draftId?: string
  } | null>(null)
  const [mode, setMode] = useState<PlaybookMode>('versions')
  const [draftMode, setDraftMode] = useState<DraftMode>('canvas')
  const [promoting, setPromoting] = useState(false)
  const [selectedStep, setSelectedStep] = useState<StepDef | null>(null)
  const [autonomy, setAutonomy] = useState<string>('agent_must_confirm')
  // Bumped on every reload so the Versions tab re-lists and re-fetches.
  const [refreshKey, setRefreshKey] = useState(0)
  // The latest live agent patch, handed to the Versions tab.
  const [patch, setPatch] = useState<{ seq: number; evt: PlaybookPatchEvt } | null>(null)
  const patchSeqRef = useRef(0)

  // Draft canvas state (playbooks render their versions' canvases in VersionsTab).
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
            candidateVersion: null,
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
            candidateVersion: pb.candidate_version ?? null,
            isDraft: false,
          })
          setRefreshKey((k) => k + 1)
        })

    load.catch((e) => setError(e.message)).finally(() => setLoading(false))
  }, [props.draftId, props.name, setNodes, setEdges])

  useEffect(() => { loadData() }, [loadData])

  // Apply ONE patch. Drafts: mutate the definition, rebuild the graph, glow
  // the touched node. Playbooks: hand it to the Versions tab, which applies
  // it to the version the agent is writing.
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
    if (!props.draftId) {
      patchSeqRef.current += 1
      setPatch({ seq: patchSeqRef.current, evt })
      return
    }
    const cur = defRef.current
    if (!cur) return
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

  const isDraft = !!meta?.isDraft
  const hasSteps = !!(definition?.steps?.length)
  const displayName = meta?.display_name || props.name || 'Untitled'

  const draftTabs: { mode: DraftMode; label: string; icon: React.ComponentType<{ className?: string }> }[] = [
    { mode: 'canvas', label: 'Canvas', icon: Eye },
    { mode: 'code', label: 'Code', icon: FileCode },
  ]
  const playbookTabs: { mode: PlaybookMode; label: string; icon: React.ComponentType<{ className?: string }> }[] = [
    { mode: 'versions', label: 'Versions', icon: History },
    { mode: 'settings', label: 'Settings', icon: Settings },
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
          <div className="flex items-center gap-1 bg-ink-900/60 rounded-lg p-0.5" data-testid="editor-tabs">
            {isDraft
              ? draftTabs.map(({ mode: m, label, icon: Icon }) => (
                  <TabBtn key={m} active={draftMode === m} onClick={() => setDraftMode(m)}>
                    <Icon className="w-3.5 h-3.5" /> {label}
                  </TabBtn>
                ))
              : playbookTabs.map(({ mode: m, label, icon: Icon }) => (
                  <TabBtn key={m} active={mode === m} onClick={() => setMode(m)}>
                    <Icon className="w-3.5 h-3.5" /> {label}
                  </TabBtn>
                ))}
          </div>
        </div>
      </div>

      {/* Content */}
      {!isDraft && props.name ? (
        mode === 'versions' ? (
          <div className="flex-1 min-h-0">
            <VersionsTab
              name={props.name}
              agentName={agentName}
              liveVersion={meta?.version ?? 0}
              candidateVersion={meta?.candidateVersion ?? null}
              refreshKey={refreshKey}
              patch={patch}
              onPromoted={() => loadData()}
              onManifestSaved={(version) => {
                setMeta((m) => (m ? { ...m, version } : m))
                setRefreshKey((k) => k + 1)
              }}
            />
          </div>
        ) : (
          <SettingsTab autonomy={autonomy} onChangeAutonomy={changeAutonomy} />
        )
      ) : (
        <div className="flex-1 min-h-0 relative flex">
          <div className="flex-1 min-w-0 relative">
            {draftMode === 'canvas' && (
              <CanvasSurface
                name={definition?.name || ''}
                explanation={definition?.explanation}
                agentName={agentName}
                hasSteps={hasSteps}
                nodes={nodes}
                edges={edges}
                onNodesChange={onNodesChange}
                onEdgesChange={onEdgesChange}
                onNodeClick={handleNodeClick}
                onPaneClick={handlePaneClick}
              />
            )}
            {draftMode === 'code' && (
              <CodeView source={sourceFor(null, definition)} agentName={agentName} />
            )}
          </div>

          {selectedStep && draftMode === 'canvas' && (
            <StepDetailPanel
              step={selectedStep}
              execRows={[]}
              hasRun={false}
              onClose={() => setSelectedStep(null)}
              onSelectStep={setSelectedStep}
              onJumpToStep={(id) => {
                const hit = findStepById(definition?.steps || [], id)
                if (hit) setSelectedStep(hit)
              }}
            />
          )}
        </div>
      )}
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

// plans/016 phase 4: Settings is a full page now (phase 6 adds the
// "Publish / Promote settings" section below "Agent can trigger").
export function SettingsTab({
  autonomy,
  onChangeAutonomy,
  children,
}: {
  autonomy: string
  onChangeAutonomy: (value: string) => void
  children?: React.ReactNode
}) {
  return (
    <div className="flex-1 min-h-0 overflow-y-auto" data-testid="settings-tab">
      <div className="max-w-2xl mx-auto py-6 px-6 space-y-8">
        <section>
          <div className="text-[11px] uppercase tracking-[0.16em] text-ink-500 mb-3">
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
        </section>
        {children}
      </div>
    </div>
  )
}
