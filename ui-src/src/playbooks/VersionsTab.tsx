/**
 * VersionsTab (plans/016 phase 4) — the playbook view. Version list on the
 * right, the selected version on the left with its own toolbar:
 *   vN · created date · Canvas | Code | Manifest | Tests | Runs · badge/Promote
 * Opens on the live version. Selection is the highlighted row — nothing is
 * ever labelled "selected"; the only row badges are `Published · Live` and
 * `Candidate`.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Eye, FileCode, FileText, FlaskConical, Play, Rocket, Loader2, X, History,
  PanelRightClose,
} from 'lucide-react'
import { cn } from '../lib/cn'
import { subscribePlaybookEvents } from '../lib/events'
import { playbooksApi, PUBLISHABLE_PLAN_STATUSES, type PlanBrief } from './api'
import { applyPlaybookPatch, type PlaybookPatchEvt } from './livePatch'
import { findStepById } from './explain/dataflow'
import { VersionCanvas, CodeView, sourceFor } from './VersionCanvas'
import { StepDetailPanel, execRowsForStep } from './StepDetailPanel'
import { ManifestTab } from './ManifestTab'
import { TestsTab } from './TestsTab'
import { RunsTab } from './RunsTab'
import { timeAgo } from './editorBits'
import type { PlaybookDef, PlaybookRunDetail, StepDef, VersionDetail } from './types'

export type VersionEntry = {
  version: number
  title: string
  author: string
  created_at: string
  runs: number
  promoted_from: number | null
  live: boolean
  candidate: boolean
  /** plans/016 phase 5: that version's spec cache. */
  specs: { total: number; failed: number; green: number }
}

export type VersionView = 'canvas' | 'code' | 'manifest' | 'tests' | 'runs'

const VIEWS: { view: VersionView; label: string; icon: React.ComponentType<{ className?: string }> }[] = [
  { view: 'canvas', label: 'Canvas', icon: Eye },
  { view: 'code', label: 'Code', icon: FileCode },
  { view: 'manifest', label: 'Manifest', icon: FileText },
  { view: 'tests', label: 'Tests', icon: FlaskConical },
  { view: 'runs', label: 'Runs', icon: Play },
]

// The view tabs adapt to the width they actually get (the version list eats
// 300px): icons + text when both fit, text only when icons would overflow,
// icons only in the tightest squeeze. Never clipped under the version list —
// two invisible measuring copies decide which variant fits.
type TabFit = 'full' | 'text' | 'icon'

function ViewTabsBar({
  mode,
  view,
  onSelect,
  interactive,
  measureRef,
}: {
  mode: TabFit
  view: VersionView
  onSelect?: (v: VersionView) => void
  interactive: boolean
  measureRef?: React.Ref<HTMLDivElement>
}) {
  return (
    <div
      ref={measureRef}
      className={cn(
        'flex items-center gap-1 bg-ink-900/60 rounded-lg p-0.5 w-fit',
        interactive
          // last resort in the very tightest squeeze: wrap to a second line,
          // never slide under the version list.
          ? 'flex-wrap max-w-full'
          : 'absolute left-0 top-0 w-max invisible pointer-events-none',
      )}
      aria-hidden={interactive ? undefined : true}
    >
      {VIEWS.map(({ view: v, label, icon: Icon }) => (
        <button
          key={v}
          onClick={interactive ? () => onSelect?.(v) : undefined}
          tabIndex={interactive ? undefined : -1}
          className={cn(
            'flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium transition whitespace-nowrap',
            interactive && view === v
              ? 'bg-luna-600/30 text-luna-200'
              : 'text-ink-400 hover:text-ink-200 hover:bg-white/5',
          )}
          data-testid={interactive ? `view-${v}` : undefined}
          title={mode === 'icon' ? label : undefined}
          aria-label={label}
        >
          {mode !== 'text' && <Icon className="w-3.5 h-3.5" />}
          {mode !== 'icon' && label}
        </button>
      ))}
    </div>
  )
}

function AdaptiveViewTabs({
  view,
  onSelect,
}: {
  view: VersionView
  onSelect: (v: VersionView) => void
}) {
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const fullRef = useRef<HTMLDivElement | null>(null)
  const textRef = useRef<HTMLDivElement | null>(null)
  const [fit, setFit] = useState<TabFit>('full')

  useEffect(() => {
    const el = wrapRef.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const measure = () => {
      const avail = el.clientWidth
      const fullW = fullRef.current?.scrollWidth ?? 0
      const textW = textRef.current?.scrollWidth ?? 0
      setFit(fullW <= avail ? 'full' : textW <= avail ? 'text' : 'icon')
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  return (
    <div ref={wrapRef} className="relative w-full min-w-0" data-testid="view-tabs">
      <ViewTabsBar mode={fit} view={view} onSelect={onSelect} interactive />
      <ViewTabsBar mode="full" view={view} interactive={false} measureRef={fullRef} />
      <ViewTabsBar mode="text" view={view} interactive={false} measureRef={textRef} />
    </div>
  )
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
      if (detail?.error) return detail.error
      if (detail?.gate) return `Promote refused — gate '${detail.gate}' failed`
    } catch { /* fall through */ }
  }
  return raw
}

/** Which gate refused, when the 422 body names one. */
export function promoteRefusalGate(err: unknown): string | null {
  const raw = err instanceof Error ? err.message : String(err)
  const jsonStart = raw.indexOf('{')
  if (jsonStart >= 0) {
    try {
      const body = JSON.parse(raw.slice(jsonStart))
      return (body.detail ?? body)?.gate ?? null
    } catch { /* fall through */ }
  }
  return null
}

function fmtAbsolute(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  return d.toLocaleString('en-US', {
    month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}

function authorLabel(a: string): string {
  return a === 'agent' ? 'agent' : a === 'owner' ? 'you' : a || '—'
}

export function LiveBadge({ className }: { className?: string }) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-medium bg-emerald-900/40 text-emerald-300 border border-emerald-500/30 whitespace-nowrap',
        className,
      )}
      data-testid="live-badge"
    >
      <span className="w-1.5 h-1.5 rounded-full bg-emerald-400" />
      Published · Live
    </span>
  )
}

function CandidateBadge({ className }: { className?: string }) {
  return (
    <span
      className={cn(
        'inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium bg-violet-900/40 text-violet-300 border border-violet-500/30 whitespace-nowrap',
        className,
      )}
      data-testid="candidate-badge"
    >
      Candidate
    </span>
  )
}

export function VersionsTab({
  name,
  agentName,
  liveVersion,
  candidateVersion,
  refreshKey = 0,
  patch = null,
  onPromoted,
  onManifestSaved,
  requireSpecs = true,
}: {
  name: string
  agentName: string
  liveVersion: number
  candidateVersion: number | null
  /** Bumped by the editor after a reload (agent save, promote) → re-list + re-fetch. */
  refreshKey?: number
  /** The latest live agent patch, forwarded by the editor's staggered queue. */
  patch?: { seq: number; evt: PlaybookPatchEvt } | null
  onPromoted: (liveVersion: number) => void
  onManifestSaved: (version: number) => void
  // plans/016 phase 6: client-side "disabled when red" only while the
  // specs gate is on (Settings → Publish).
  requireSpecs?: boolean
}) {
  const [versions, setVersions] = useState<VersionEntry[] | null>(null)
  const [selected, setSelected] = useState<number | null>(null)
  const [detail, setDetail] = useState<VersionDetail | null>(null)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [view, setView] = useState<VersionView>('canvas')
  const [runDetail, setRunDetail] = useState<PlaybookRunDetail | null>(null)
  const [selectedStep, setSelectedStep] = useState<StepDef | null>(null)
  const [promoting, setPromoting] = useState(false)
  const [promoteError, setPromoteError] = useState<string | null>(null)
  // plans/016 phase 3: every promote carries a plan. The Promote click opens
  // a picker over publishable plans; a test_run refusal offers Promote anyway
  // (the owner's own click is the consent).
  const [planPickerOpen, setPlanPickerOpen] = useState(false)
  const [planOptions, setPlanOptions] = useState<PlanBrief[] | null>(null)
  const [forceOffer, setForceOffer] = useState<string | null>(null) // plan_id to retry with
  // The version list folds away to a slim rail when the owner wants the room.
  const [listOpen, setListOpen] = useState(true)
  // Live agent edits applied on top of the fetched definition (with glow).
  const [patchedDef, setPatchedDef] = useState<PlaybookDef | null>(null)
  const [glow, setGlow] = useState<Map<string, number>>(new Map())
  const glowSeqRef = useRef(0)

  const reList = useCallback(() => {
    return playbooksApi.listVersions(name)
      .then((rows) => {
        const list: VersionEntry[] = rows.map((r: any) => ({
          version: r.version,
          title: r.title,
          author: r.author,
          created_at: r.created_at,
          runs: r.runs,
          promoted_from: r.promoted_from,
          live: !!(r.live ?? r.current),
          specs: r.specs ?? { total: 0, failed: 0, green: 0 },
          candidate: !!r.candidate,
        }))
        setVersions(list)
        return list
      })
      .catch(() => { setVersions([]); return [] as VersionEntry[] })
  }, [name])

  // List: on mount and whenever the editor reloads. Selection falls back to
  // the live version when nothing (or something gone) is selected.
  useEffect(() => {
    reList().then((list) => {
      setSelected((cur) => {
        if (cur != null && list.some((v) => v.version === cur)) return cur
        const live = list.find((v) => v.live)
        return live ? live.version : liveVersion
      })
    })
  }, [reList, refreshKey, liveVersion])

  // Selected version's content.
  useEffect(() => {
    if (selected == null) return
    let cancelled = false
    setDetailError(null)
    playbooksApi.getVersion(name, selected)
      .then((d) => {
        if (cancelled) return
        setDetail(d)
        setPatchedDef(null)
        setGlow(new Map())
        setSelectedStep(null)
      })
      .catch((e) => { if (!cancelled) setDetailError(e.message) })
    return () => { cancelled = true }
  }, [name, selected, refreshKey])

  // Live agent patches follow the version the agent is writing: the candidate,
  // or live when there is no candidate yet.
  useEffect(() => {
    if (!patch || !detail) return
    const follows = detail.candidate || (candidateVersion == null && detail.live)
    if (!follows) return
    const base = patchedDef ?? detail.definition
    const { def, glowNodeId } = applyPlaybookPatch(base, patch.evt)
    setPatchedDef(def)
    setRunDetail(null)
    if (glowNodeId) {
      glowSeqRef.current += 1
      setGlow((g) => new Map(g).set(glowNodeId, glowSeqRef.current))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [patch])

  const selectVersion = useCallback((n: number) => {
    if (n === selected) return
    setSelected(n)
    setRunDetail(null)
    setPromoteError(null)
    setForceOffer(null)
    setPlanPickerOpen(false)
  }, [selected])

  const loadRun = useCallback(async (runId: string, { switchView = true } = {}) => {
    try {
      const run = await playbooksApi.getRun(runId)
      const v = (run as any).playbook_version as number | undefined
      if (v != null && v !== selected) {
        setSelected(v)
        setPromoteError(null)
      }
      setRunDetail(run)
      if (switchView) setView('canvas')
    } catch { /* ignore */ }
  }, [selected])

  const refreshRunStatuses = useCallback(async (runId: string) => {
    try {
      setRunDetail(await playbooksApi.getRun(runId))
    } catch { /* ignore */ }
  }, [])

  // Live attach: a run of THIS playbook started elsewhere (chat tool, trigger,
  // cron) — show it on its version's canvas; step events nudge a refresh.
  const runDetailRef = useRef<PlaybookRunDetail | null>(null)
  useEffect(() => { runDetailRef.current = runDetail }, [runDetail])
  useEffect(() => {
    return subscribePlaybookEvents((evt) => {
      const current = runDetailRef.current
      if (evt.event === 'playbook.run.started') {
        if (evt.playbook_name === name && (!current || current.status !== 'running')) {
          void loadRun(evt.run_id)
        }
        return
      }
      if (evt.event === 'playbook.run.completed' && evt.playbook_name === name) {
        void reList()
      }
      if (current && evt.run_id === current.id) void refreshRunStatuses(evt.run_id)
    })
  }, [name, loadRun, refreshRunStatuses, reList])

  useEffect(() => {
    if (!runDetail || runDetail.status !== 'running') return
    const t = setTimeout(() => { void refreshRunStatuses(runDetail.id) }, 1400)
    return () => clearTimeout(t)
  }, [runDetail, refreshRunStatuses])

  const openPlanPicker = () => {
    if (!detail || promoting) return
    setPromoteError(null)
    setForceOffer(null)
    setPlanPickerOpen(true)
    setPlanOptions(null)
    playbooksApi.listPlans()
      .then((r) => {
        const usable = r.plans.filter((p) => PUBLISHABLE_PLAN_STATUSES.includes(p.status))
        // Plans naming THIS playbook first; the server already sorts newest first.
        usable.sort((a, b) =>
          Number(b.playbook_refs.includes(name)) - Number(a.playbook_refs.includes(name)))
        setPlanOptions(usable)
      })
      .catch(() => setPlanOptions([]))
  }

  const promoteWithPlan = async (planId: string, forceTestRun = false) => {
    if (!detail || promoting) return
    setPromoting(true)
    setPromoteError(null)
    setForceOffer(null)
    setPlanPickerOpen(false)
    try {
      const n = detail.version
      if (detail.candidate) await playbooksApi.promoteCandidate(name, planId, forceTestRun)
      else await playbooksApi.promoteVersion(name, n, planId, forceTestRun)
      setSelected(n)
      onPromoted(n)
    } catch (e) {
      setPromoteError(promoteRefusalMessage(e))
      if (!forceTestRun && promoteRefusalGate(e) === 'test_run') setForceOffer(planId)
    } finally {
      setPromoting(false)
    }
  }

  const def = patchedDef ?? detail?.definition ?? null
  const isLive = !!detail?.live
  // Known-red specs of the selected version (from the list's cache) block
  // Promote up front — the server gate would refuse anyway.
  const redSpecs = requireSpecs
    ? (versions?.find((v) => v.version === selected)?.specs.failed ?? 0)
    : 0

  return (
    <div className="h-full flex min-h-0">
      {/* Left: the selected version */}
      <div className="flex-1 min-w-0 flex flex-col">
        {detail && (
          <div
            className="px-4 pt-2.5 pb-2 border-b border-white/5 shrink-0 space-y-2"
            data-testid="version-toolbar"
          >
            {/* Row 1: version · date · (badge | promote) — wraps downward
                rather than sliding under the version list. */}
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
            <span className="text-lg font-bold text-ink-50 leading-none" data-testid="toolbar-version">
              v{detail.version}
            </span>
            <span className="text-[11px] text-ink-500 whitespace-nowrap" title={timeAgo(detail.created_at)}>
              {fmtAbsolute(detail.created_at)}
            </span>
            <div className="flex-1" />
            {isLive ? (
              <LiveBadge className="px-2.5 py-1 text-[11px]" />
            ) : (
              <div className="relative">
                <button
                  onClick={openPlanPicker}
                  disabled={promoting || redSpecs > 0}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 disabled:opacity-40 text-white text-xs font-medium transition whitespace-nowrap"
                  title={redSpecs > 0
                    ? `${redSpecs} of this version's tests ${redSpecs === 1 ? 'is' : 'are'} red — fix or re-run them first (Tests view)`
                    : detail.candidate
                      ? 'Run the gates (tests, tool checks) and make this candidate live'
                      : 'Make this version live again'}
                  data-testid="promote-btn"
                >
                  {promoting ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Rocket className="w-3.5 h-3.5" />}
                  Promote to live
                </button>
                {planPickerOpen && (
                  <div
                    className="absolute right-0 top-full mt-1.5 w-[320px] z-20 rounded-lg border border-white/10 bg-ink-900 shadow-xl p-3"
                    data-testid="plan-picker"
                  >
                    <div className="flex items-center justify-between mb-2">
                      <span className="text-[11px] uppercase tracking-[0.16em] text-ink-500">
                        Which plan is this for?
                      </span>
                      <button
                        onClick={() => setPlanPickerOpen(false)}
                        className="p-0.5 rounded hover:bg-white/10 text-ink-500 hover:text-ink-200 transition"
                        title="Cancel"
                      >
                        <X className="w-3 h-3" />
                      </button>
                    </div>
                    {planOptions === null ? (
                      <div className="flex justify-center py-4">
                        <Loader2 className="w-4 h-4 animate-spin text-ink-500" />
                      </div>
                    ) : planOptions.length === 0 ? (
                      <p className="text-xs text-ink-400" data-testid="plan-picker-empty">
                        No open plan covers this change. Ask {agentName} in chat to
                        write one — every playbook change needs a plan you can read.
                      </p>
                    ) : (
                      <div className="space-y-1 max-h-64 overflow-y-auto">
                        {planOptions.map((p) => (
                          <button
                            key={p.plan_id}
                            onClick={() => void promoteWithPlan(p.plan_id)}
                            className="w-full text-left rounded-md px-2.5 py-2 hover:bg-white/[.05] transition"
                            data-testid={`plan-option-${p.plan_id}`}
                          >
                            <span className="text-xs font-medium text-ink-100 block truncate">
                              {p.title}
                            </span>
                            <span className="text-[10px] text-ink-500">
                              {p.playbook_refs.includes(name) ? `${name} · ` : ''}
                              {p.created_at ? timeAgo(p.created_at) : ''}
                            </span>
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            )}
            </div>
            {/* Row 2: view tabs, left-aligned, adapting to the room they get */}
            <AdaptiveViewTabs view={view} onSelect={setView} />
          </div>
        )}

        {promoteError && (
          <div
            className="flex items-start gap-2 px-4 py-2 border-b border-rose-500/20 bg-rose-950/30 shrink-0"
            data-testid="promote-error"
          >
            <p className="text-xs text-rose-300 flex-1">{promoteError}</p>
            {forceOffer && (
              <button
                onClick={() => void promoteWithPlan(forceOffer, true)}
                disabled={promoting}
                className="shrink-0 px-2.5 py-1 rounded-md border border-rose-400/40 text-rose-200 hover:bg-rose-500/15 disabled:opacity-40 text-[11px] font-medium transition whitespace-nowrap"
                title="Skip the test-run check just this once — everything else was checked"
                data-testid="promote-anyway-btn"
              >
                Promote anyway
              </button>
            )}
            <button
              onClick={() => { setPromoteError(null); setForceOffer(null) }}
              className="p-0.5 rounded hover:bg-white/10 text-rose-400/70 hover:text-rose-200 transition"
              title="Dismiss"
            >
              <X className="w-3 h-3" />
            </button>
          </div>
        )}

        <div className="flex-1 min-h-0 relative flex">
          <div className="flex-1 min-w-0 relative">
            {detailError ? (
              <div className="h-full flex items-center justify-center text-rose-400 text-sm">{detailError}</div>
            ) : !detail ? (
              <div className="h-full flex items-center justify-center text-ink-400">
                <Loader2 className="w-5 h-5 animate-spin" />
              </div>
            ) : view === 'canvas' ? (
              <VersionCanvas
                def={def}
                name={name}
                agentName={agentName}
                runDetail={runDetail}
                onClearRun={() => setRunDetail(null)}
                onSelectStep={setSelectedStep}
                glow={glow}
              />
            ) : view === 'code' ? (
              <CodeView source={sourceFor(detail.code, def)} agentName={agentName} />
            ) : view === 'manifest' ? (
              isLive ? (
                <ManifestTab name={name} onSaved={onManifestSaved} />
              ) : (
                <div className="h-full overflow-y-auto">
                  <div className="max-w-2xl mx-auto py-4 px-4">
                    <div className="text-[11px] uppercase tracking-[0.16em] text-ink-500 mb-1">
                      Manifest · v{detail.version}
                    </div>
                    <p className="text-[11px] text-ink-600 mb-3">
                      Snapshot — only the live version's manifest can be edited.
                    </p>
                    <pre
                      className="text-xs text-ink-200 whitespace-pre-wrap bg-ink-900/60 rounded-lg p-3 border border-white/5"
                      data-testid="manifest-snapshot"
                    >
                      {detail.manifest || '(empty)'}
                    </pre>
                  </div>
                </div>
              )
            ) : view === 'tests' ? (
              <TestsTab name={name} version={detail.version} />
            ) : (
              <RunsTab
                name={name}
                version={detail.version}
                onShowOnCanvas={(runId) => { void loadRun(runId) }}
              />
            )}
          </div>

          {selectedStep && view === 'canvas' && detail && (
            <StepDetailPanel
              step={selectedStep}
              execRows={execRowsForStep(runDetail, selectedStep.id)}
              hasRun={!!runDetail}
              onClose={() => setSelectedStep(null)}
              onSelectStep={setSelectedStep}
              onJumpToStep={(id) => {
                const hit = findStepById(def?.steps || [], id)
                if (hit) setSelectedStep(hit)
              }}
            />
          )}
        </div>
      </div>

      {/* Right: the version list — folds to a slim rail on demand */}
      {!listOpen ? (
        <div
          className="w-10 shrink-0 border-l border-white/5 bg-ink-950/60 flex flex-col items-center pt-2 gap-1"
          data-testid="version-list-collapsed"
        >
          <button
            onClick={() => setListOpen(true)}
            className="p-1.5 rounded-lg hover:bg-white/5 text-ink-400 hover:text-ink-100 transition"
            title="Show versions"
            data-testid="versions-expand"
          >
            <History className="w-4 h-4" />
          </button>
          {versions && (
            <span className="text-[10px] text-ink-500">{versions.length}</span>
          )}
        </div>
      ) : (
      <div
        className="w-[300px] shrink-0 border-l border-white/5 bg-ink-950/60 overflow-y-auto"
        data-testid="version-list"
      >
        <div className="flex items-center gap-2 px-4 py-3 border-b border-white/5">
          <History className="w-4 h-4 text-ink-400" />
          <span className="text-sm font-medium text-ink-100">Versions</span>
          {versions && (
            <span className="text-[11px] text-ink-500 ml-auto">{versions.length}</span>
          )}
          <button
            onClick={() => setListOpen(false)}
            className="p-1 rounded-md hover:bg-white/5 text-ink-500 hover:text-ink-200 transition"
            title="Hide versions"
            data-testid="versions-collapse"
          >
            <PanelRightClose className="w-4 h-4" />
          </button>
        </div>
        <div className="px-2 py-2">
          {versions === null ? (
            <div className="flex items-center justify-center py-8 text-ink-500">
              <Loader2 className="w-4 h-4 animate-spin" />
            </div>
          ) : versions.length === 0 ? (
            <p className="text-xs text-ink-500 text-center py-8">No version history yet</p>
          ) : (
            <div className="space-y-1">
              {versions.map((v) => {
                const active = v.version === selected
                return (
                  <button
                    key={v.version}
                    onClick={() => selectVersion(v.version)}
                    className={cn(
                      'w-full text-left rounded-lg px-3 py-2.5 transition',
                      active
                        ? 'bg-luna-600/20 text-luna-200'
                        : 'hover:bg-white/[.03] text-ink-300',
                    )}
                    data-testid={`version-row-${v.version}`}
                    aria-current={active ? 'true' : undefined}
                  >
                    <div className="flex items-start gap-2">
                      <span className={cn('text-sm font-bold', active ? 'text-luna-100' : 'text-ink-100')}>
                        v{v.version}
                      </span>
                      <span className="ml-auto shrink-0">
                        {v.live ? <LiveBadge /> : v.candidate ? <CandidateBadge /> : null}
                      </span>
                    </div>
                    {v.title && (
                      <p className="text-xs text-ink-400 mt-1 truncate" title={v.title}>
                        "{v.title}"
                      </p>
                    )}
                    <div className="flex items-center gap-1.5 mt-1.5 text-[11px] text-ink-500">
                      <span>{authorLabel(v.author)}</span>
                      <span>·</span>
                      <span title={fmtAbsolute(v.created_at)}>{timeAgo(v.created_at)}</span>
                      <span>·</span>
                      <span>{v.runs} {v.runs === 1 ? 'run' : 'runs'}</span>
                    </div>
                    {v.specs.total > 0 && (
                      <div
                        className={cn(
                          'text-[11px] mt-1',
                          v.specs.failed > 0 ? 'text-rose-400' : v.specs.green === v.specs.total ? 'text-emerald-400' : 'text-ink-500',
                        )}
                        data-testid={`version-specs-${v.version}`}
                      >
                        {v.specs.total} {v.specs.total === 1 ? 'test' : 'tests'} ·{' '}
                        {v.specs.failed > 0
                          ? `${v.specs.failed} red`
                          : v.specs.green === v.specs.total
                            ? `${v.specs.green} green`
                            : `${v.specs.green} green, ${v.specs.total - v.specs.green} not run`}
                      </div>
                    )}
                    {v.promoted_from != null && (
                      <p className="text-[10px] text-ink-600 mt-1">← promoted from v{v.promoted_from}</p>
                    )}
                  </button>
                )
              })}
            </div>
          )}
        </div>
      </div>
      )}
    </div>
  )
}
