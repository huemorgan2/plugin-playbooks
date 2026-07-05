import { useEffect, useMemo, useState } from 'react'
import { Loader2, Play, CircleDot, Clock, AlertTriangle, CheckCircle2, XCircle, X } from 'lucide-react'
import { cn } from '../lib/cn'
import { playbooksApi } from './api'
import type { PlaybookRunSummary, RunStatus } from './types'

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

// 006.714: rows show "X ago"; the absolute date/time lives in the tooltip (and
// on the canvas replay banner once a run is loaded).
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

export function PlaybookRuns({
  name,
  activeRunId,
  onLoadRun,
  onPlayRun,
  onClose,
}: {
  name: string
  activeRunId?: string | null
  onLoadRun: (runId: string) => void
  onPlayRun: (runId: string) => void
  onClose: () => void
}) {
  const [runs, setRuns] = useState<PlaybookRunSummary[]>([])
  const [loading, setLoading] = useState(true)

  const stats = useMemo(() => computeStats(runs), [runs])

  useEffect(() => {
    setLoading(true)
    playbooksApi
      .listRuns(name)
      .then(setRuns)
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [name])

  return (
    <div className="w-[340px] shrink-0 border-l border-white/5 bg-ink-950/80 backdrop-blur-sm flex flex-col">
      <div className="flex items-center justify-between px-4 py-3 border-b border-white/5 shrink-0">
        <div className="flex items-center gap-2">
          <Play className="w-4 h-4 text-luna-300" />
          <span className="text-xs font-semibold text-ink-100">Runs</span>
        </div>
        <button
          onClick={onClose}
          className="p-1 rounded hover:bg-white/10 text-ink-500 hover:text-ink-200 transition"
        >
          <X className="w-3.5 h-3.5" />
        </button>
      </div>

      {loading ? (
        <div className="flex items-center justify-center flex-1 text-ink-400">
          <Loader2 className="w-5 h-5 animate-spin" />
        </div>
      ) : runs.length === 0 ? (
        <div className="flex flex-col items-center justify-center flex-1 text-ink-500 gap-2 px-4 text-center">
          <Play className="w-8 h-8 text-ink-600" />
          <p className="text-sm">No runs yet</p>
          <p className="text-[11px] text-ink-600">Run this playbook and its results show up here.</p>
        </div>
      ) : (
        <>
          <RunStatsBar stats={stats} />
          <div className="flex-1 overflow-y-auto">
            {runs.map((run) => {
              const StatusIcon = STATUS_ICON[run.status] || CircleDot
              const dur = fmtDuration(run.started_at, run.completed_at)
              const isActive = activeRunId === run.id
              return (
                <div
                  key={run.id}
                  className={cn(
                    'w-full flex items-center gap-2.5 px-4 py-3 border-b border-white/5 transition',
                    isActive ? 'bg-luna-600/15' : 'hover:bg-white/[.02]',
                  )}
                >
                  <button
                    onClick={() => onPlayRun(run.id)}
                    className="shrink-0 p-1.5 rounded-full text-ink-400 hover:bg-white/10 hover:text-luna-300 transition"
                    title={`Replay this run from ${fmtRelative(run.started_at)}`}
                    data-testid="run-play-btn"
                  >
                    <Play className="w-3.5 h-3.5" />
                  </button>
                  <button
                    onClick={() => onLoadRun(run.id)}
                    className="flex-1 min-w-0 flex items-center gap-2.5 text-left"
                    title={run.started_at ? new Date(run.started_at).toLocaleString() : 'Pending'}
                  >
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
                </div>
              )
            })}
          </div>
        </>
      )}
    </div>
  )
}

function RunStatsBar({ stats }: { stats: ReturnType<typeof computeStats> }) {
  return (
    <div
      data-testid="run-stats-bar"
      className="flex items-center gap-4 px-4 py-2.5 border-b border-white/10 bg-ink-900/30 text-xs shrink-0"
    >
      <Stat label="Total" value={stats.total} className="text-ink-200" />
      <Stat label="OK" value={stats.success} className="text-emerald-400" />
      <Stat label="Failed" value={stats.failed} className="text-rose-400" />
      <Stat label="Other" value={stats.other} className="text-ink-400" />
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
