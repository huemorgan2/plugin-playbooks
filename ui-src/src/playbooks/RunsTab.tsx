/**
 * RunsTab (0.13.0, plans/002 phase 6) — the run list moved from the side
 * panel into an editor tab. Each run expands into its step execution list
 * (what actually happened, step by step); replay/animation is gone —
 * "Show on canvas" colors the graph by status instead.
 */
import { useEffect, useMemo, useState } from 'react'
import {
  Loader2, Play, CircleDot, Clock, AlertTriangle, CheckCircle2, XCircle,
  ChevronDown, ChevronRight, Workflow,
} from 'lucide-react'
import { cn } from '../lib/cn'
import { subscribePlaybookEvents } from '../lib/events'
import { playbooksApi } from './api'
import type { PlaybookRunSummary, PlaybookRunDetail, StepRunDetail, RunStatus } from './types'

const SUCCESS_STATUSES: RunStatus[] = ['completed', 'done']
const FAIL_STATUSES: RunStatus[] = ['failed']

function computeStats(runs: PlaybookRunSummary[]) {
  let success = 0
  let failed = 0
  let other = 0
  for (const r of runs) {
    if (SUCCESS_STATUSES.includes(r.status)) success++
    else if (FAIL_STATUSES.includes(r.status)) failed++
    else other++
  }
  return { total: runs.length, success, failed, other }
}

const STATUS_ICON: Record<RunStatus, React.ComponentType<{ className?: string }>> = {
  pending: Clock,
  running: Play,
  completed: CheckCircle2,
  done: CheckCircle2,
  failed: XCircle,
  waiting: Clock,
  cancelled: AlertTriangle,
}

const STATUS_CLASS: Record<RunStatus, string> = {
  pending: 'text-ink-400',
  running: 'text-blue-400 animate-pulse',
  completed: 'text-emerald-400',
  done: 'text-emerald-400',
  failed: 'text-rose-400',
  waiting: 'text-amber-400',
  cancelled: 'text-ink-500',
}

function fmtRelative(iso: string | null): string {
  if (!iso) return 'pending'
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return ''
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

function fmtDuration(start: string | null, end: string | null): string | null {
  if (!start || !end) return null
  const ms = new Date(end).getTime() - new Date(start).getTime()
  if (Number.isNaN(ms) || ms < 0) return null
  if (ms < 1000) return `${ms}ms`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)}s`
  const m = Math.floor(s / 60)
  const rem = Math.round(s % 60)
  return `${m}m ${rem}s`
}

function triggerLabel(trigger: string): string {
  if (!trigger || trigger === 'manual') return 'Manual run'
  if (trigger === 'agent') return 'Agent run'
  if (trigger === 'cron' || trigger === 'schedule') return 'Scheduled run'
  return `Trigger: ${trigger}`
}

export function RunsTab({
  name,
  onShowOnCanvas,
}: {
  name: string
  onShowOnCanvas: (runId: string) => void
}) {
  const [runs, setRuns] = useState<PlaybookRunSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [openRunId, setOpenRunId] = useState<string | null>(null)

  const stats = useMemo(() => computeStats(runs), [runs])

  useEffect(() => {
    setLoading(true)
    playbooksApi
      .listRuns(name)
      .then(setRuns)
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [name])

  // live: a run started or finished anywhere (chat, trigger, cron) — refetch
  // so the tab isn't frozen at its mount-time snapshot
  useEffect(() => {
    return subscribePlaybookEvents((evt) => {
      if (evt.event !== 'playbook.run.started' && evt.event !== 'playbook.run.completed') return
      playbooksApi.listRuns(name).then(setRuns).catch(() => {})
    })
  }, [name])

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full text-ink-400">
        <Loader2 className="w-5 h-5 animate-spin" />
      </div>
    )
  }

  if (runs.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-ink-500 gap-2 px-4 text-center">
        <Play className="w-8 h-8 text-ink-600" />
        <p className="text-sm">No runs yet</p>
        <p className="text-[11px] text-ink-600">Run this playbook and its results show up here.</p>
      </div>
    )
  }

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-2xl mx-auto py-4 px-4">
        <div className="text-[11px] uppercase tracking-[0.16em] text-ink-500 mb-1">Runs</div>
        <div
          data-testid="run-stats-bar"
          className="flex items-center gap-4 py-2 text-xs"
        >
          <Stat label="Total" value={stats.total} className="text-ink-200" />
          <Stat label="OK" value={stats.success} className="text-emerald-400" />
          <Stat label="Failed" value={stats.failed} className="text-rose-400" />
          <Stat label="Other" value={stats.other} className="text-ink-400" />
        </div>
        <div className="mt-2 rounded-xl border border-white/5 overflow-hidden">
          {runs.map((run) => (
            <RunRow
              key={run.id}
              run={run}
              open={openRunId === run.id}
              onToggle={() => setOpenRunId((cur) => (cur === run.id ? null : run.id))}
              onShowOnCanvas={() => onShowOnCanvas(run.id)}
            />
          ))}
        </div>
      </div>
    </div>
  )
}

function RunRow({
  run, open, onToggle, onShowOnCanvas,
}: {
  run: PlaybookRunSummary
  open: boolean
  onToggle: () => void
  onShowOnCanvas: () => void
}) {
  const StatusIcon = STATUS_ICON[run.status] || CircleDot
  const dur = fmtDuration(run.started_at, run.completed_at)
  const [detail, setDetail] = useState<PlaybookRunDetail | null>(null)
  const [loadingDetail, setLoadingDetail] = useState(false)

  useEffect(() => {
    if (!open || detail) return
    setLoadingDetail(true)
    playbooksApi.getRun(run.id)
      .then(setDetail)
      .catch(() => {})
      .finally(() => setLoadingDetail(false))
  }, [open, detail, run.id])

  return (
    <div className="border-b border-white/5 last:border-b-0" data-testid="run-row">
      <button
        onClick={onToggle}
        className={cn(
          'w-full flex items-center gap-2.5 px-4 py-3 text-left transition',
          open ? 'bg-white/[.03]' : 'hover:bg-white/[.02]',
        )}
        title={run.started_at ? new Date(run.started_at).toLocaleString() : 'Pending'}
      >
        {open ? (
          <ChevronDown className="w-3.5 h-3.5 text-ink-500 shrink-0" />
        ) : (
          <ChevronRight className="w-3.5 h-3.5 text-ink-500 shrink-0" />
        )}
        <StatusIcon className={cn('w-4 h-4 shrink-0', STATUS_CLASS[run.status])} />
        <div className="flex-1 min-w-0">
          <div className="text-xs font-medium text-ink-200 truncate">
            {triggerLabel(run.trigger)}
          </div>
          <div className="text-[11px] text-ink-500 mt-0.5">
            {fmtRelative(run.started_at)}
            {dur ? ` · ${dur}` : ''}
          </div>
        </div>
        <span
          className={cn(
            'text-[10px] font-medium px-1.5 py-0.5 rounded shrink-0',
            run.status === 'completed' || run.status === 'done' ? 'bg-emerald-900/40 text-emerald-400' :
            run.status === 'failed' ? 'bg-rose-900/40 text-rose-400' :
            run.status === 'running' ? 'bg-blue-900/40 text-blue-400' :
            'bg-ink-800 text-ink-400'
          )}
        >
          {run.status}
        </span>
      </button>

      {open && (
        <div className="px-4 pb-3 pl-10">
          {loadingDetail ? (
            <div className="flex items-center gap-2 py-3 text-ink-500 text-xs">
              <Loader2 className="w-3.5 h-3.5 animate-spin" /> Loading steps…
            </div>
          ) : !detail ? (
            <p className="text-xs text-ink-600 italic py-2">Could not load this run's steps.</p>
          ) : (
            <>
              <button
                onClick={onShowOnCanvas}
                className="inline-flex items-center gap-1.5 mb-2 px-2 py-1 rounded text-[11px] font-medium text-luna-400 hover:bg-luna-600/20 transition"
                data-testid="run-show-on-canvas"
              >
                <Workflow className="w-3.5 h-3.5" />
                Show on canvas
              </button>
              {detail.steps.length === 0 ? (
                <p className="text-xs text-ink-600 italic">No steps were executed.</p>
              ) : (
                <div className="space-y-px">
                  {detail.steps.map((s, i) => (
                    <StepExecRow key={`${s.step_id}-${i}`} step={s} />
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

function StepExecRow({ step }: { step: StepRunDetail }) {
  const [open, setOpen] = useState(false)
  const dur = fmtDuration(step.started_at, step.completed_at)
  const hasDetail = !!(step.error
    || (step.inputs && Object.keys(step.inputs).length > 0)
    || step.outputs != null)
  return (
    <div className="rounded-md" data-testid="step-exec-row">
      <button
        onClick={() => hasDetail && setOpen((v) => !v)}
        className={cn(
          'w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-left transition',
          hasDetail ? 'hover:bg-white/[.03] cursor-pointer' : 'cursor-default',
        )}
      >
        {hasDetail ? (
          open
            ? <ChevronDown className="w-3 h-3 text-ink-600 shrink-0" />
            : <ChevronRight className="w-3 h-3 text-ink-600 shrink-0" />
        ) : (
          <span className="w-3 shrink-0" />
        )}
        <span className={cn('w-1.5 h-1.5 rounded-full shrink-0',
          step.status === 'failed' ? 'bg-rose-400' :
          step.status === 'running' ? 'bg-blue-400 animate-pulse' :
          step.status === 'waiting' ? 'bg-amber-400' :
          step.status === 'cancelled' ? 'bg-ink-500' :
          'bg-emerald-400',
        )} />
        <span className="text-xs font-mono text-ink-200 truncate">{step.step_id}</span>
        <span className="text-[10px] text-ink-600 capitalize shrink-0">{step.kind.replace(/_/g, ' ')}</span>
        <span className="flex-1" />
        {step.retry_count != null && step.retry_count > 0 && (
          <span className="text-[10px] text-amber-400 shrink-0">
            {step.retry_count} retr{step.retry_count === 1 ? 'y' : 'ies'}
          </span>
        )}
        {step.cost_cents != null && step.cost_cents > 0 && (
          <span className="text-[10px] text-ink-500 shrink-0">${(step.cost_cents / 100).toFixed(4)}</span>
        )}
        {dur && <span className="text-[10px] text-ink-500 shrink-0">{dur}</span>}
      </button>
      {open && hasDetail && (
        <div className="ml-7 mb-2 space-y-2">
          {step.error && (
            <div>
              <div className="text-[10px] text-ink-500 mb-0.5">Error</div>
              <pre className="text-[10px] text-rose-300 font-mono whitespace-pre-wrap bg-rose-950/30 rounded p-2 max-h-40 overflow-auto">
                {step.error}
              </pre>
            </div>
          )}
          {step.inputs && Object.keys(step.inputs).length > 0 && (
            <div>
              <div className="text-[10px] text-ink-500 mb-0.5">Resolved inputs</div>
              <pre className="text-[10px] text-ink-300 font-mono whitespace-pre-wrap bg-ink-900/60 rounded p-2 max-h-48 overflow-auto">
                {JSON.stringify(step.inputs, null, 2)}
              </pre>
            </div>
          )}
          {step.outputs != null && (
            <div>
              <div className="text-[10px] text-ink-500 mb-0.5">Output</div>
              <pre className="text-[10px] text-ink-300 font-mono whitespace-pre-wrap bg-ink-900/60 rounded p-2 max-h-56 overflow-auto">
                {JSON.stringify(step.outputs, null, 2)}
              </pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function Stat({ label, value, className }: { label: string; value: number; className?: string }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <span className={cn('text-sm font-semibold tabular-nums', className)}>{value}</span>
      <span className="text-[11px] text-ink-500">{label}</span>
    </div>
  )
}
