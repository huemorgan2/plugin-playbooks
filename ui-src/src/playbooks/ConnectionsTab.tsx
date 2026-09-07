/**
 * ConnectionsTab (0.13.0, plans/002 phase 6; 0.47.0: the stored-tests half
 * of the old Tests tab was removed with the specs feature) — the playbook's
 * tool-health page: eyebrow → bottom-line headline → one-line rows of the
 * cached tool probes, with Check now.
 * Failure classes render as plain words (trust.ts), never protocol codes.
 */
import { useCallback, useEffect, useState } from 'react'
import { Loader2, RefreshCw } from 'lucide-react'
import { cn } from '../lib/cn'
import { playbooksApi } from './api'
import { IntegrationIcon, toolIconUrl, useIconRef } from './icons'
import type { ProbeEntry } from './types'
import { probesHeadline, failureWords, TONE_TEXT } from './trust'

function fmtRelative(iso: string | null): string {
  if (!iso) return ''
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

function Dot({ tone }: { tone: 'ok' | 'warn' | 'bad' }) {
  return (
    <span className={cn('w-[7px] h-[7px] rounded-full shrink-0',
      tone === 'ok' ? 'bg-emerald-400' : tone === 'bad' ? 'bg-rose-400' : 'bg-amber-400',
    )} />
  )
}

// Probes are per playbook, not per version — the tab is rendered inside the
// Versions view for layout parity with Manifest and Runs.
export function ConnectionsTab({ name }: { name: string }) {
  const [probes, setProbes] = useState<ProbeEntry[] | null>(null)
  const [runningPreflight, setRunningPreflight] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(() => {
    playbooksApi.getProbes(name).then((r) => setProbes(r.probes)).catch(() => setProbes([]))
  }, [name])

  useEffect(() => { refresh() }, [refresh])

  const checkNow = async () => {
    if (runningPreflight) return
    setRunningPreflight(true)
    setError(null)
    try {
      await playbooksApi.runPreflight(name)
      refresh()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setRunningPreflight(false)
    }
  }

  if (probes === null) {
    return (
      <div className="flex items-center justify-center h-full text-ink-400">
        <Loader2 className="w-5 h-5 animate-spin" />
      </div>
    )
  }

  const probeFailed = probes.filter((p) => p.status === 'failed').length
  const probeOk = probes.filter((p) => p.status === 'ok').length
  const probeVerdict = probesHeadline(probes.length, probeOk, probeFailed)

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-2xl mx-auto py-4 px-4 space-y-8">
        {error && (
          <p className="text-xs text-rose-400">{error}</p>
        )}

        {/* CONNECTIONS */}
        <section>
          <div className="flex items-center justify-between">
            <div
              className="text-[11px] uppercase tracking-[0.16em] text-ink-500"
              data-testid="connections-header"
            >
              Connections
            </div>
            <button
              onClick={checkNow}
              disabled={runningPreflight}
              className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[11px] font-medium text-luna-400 hover:bg-luna-600/20 disabled:opacity-40 transition"
              data-testid="probes-check-now"
            >
              {runningPreflight ? <Loader2 className="w-3 h-3 animate-spin" /> : <RefreshCw className="w-3 h-3" />}
              Check now
            </button>
          </div>
          <div
            className={cn('text-xl font-semibold mt-1', TONE_TEXT[probeVerdict.tone])}
            data-testid="probes-headline"
          >
            {probeVerdict.text}
          </div>
          <p className="text-xs text-ink-500 mt-0.5">
            Every tool this playbook touches gets asked "are you still working?"
          </p>
          {probes.length > 0 && (
            <div className="mt-3 rounded-xl border border-white/5 overflow-hidden">
              {probes.map((p) => <ProbeRow key={p.tool} probe={p} />)}
            </div>
          )}
        </section>
      </div>
    </div>
  )
}

function ProbeRow({ probe }: { probe: ProbeEntry }) {
  const iconRef = useIconRef()
  const toolUrl = toolIconUrl(iconRef, probe.tool)
  const tone = probe.status === 'failed' ? 'bad' as const
    : probe.status === 'ok' ? 'ok' as const : 'warn' as const
  const label = probe.status === 'failed'
    ? failureWords(probe.failure_class)
    : probe.status === 'ok' ? 'working' : "can't be checked"
  return (
    <div
      className="flex items-center gap-2.5 px-3 py-2.5 border-b border-white/5 last:border-b-0"
      data-testid="probe-row"
      title={probe.detail || undefined}
    >
      <Dot tone={tone} />
      {toolUrl && <IntegrationIcon url={toolUrl} fallback={() => null} className="w-4 h-4" />}
      <span className="text-xs font-mono text-ink-200 truncate flex-1">{probe.tool}</span>
      <span className={cn('text-[10px] shrink-0',
        tone === 'bad' ? 'text-rose-400' : tone === 'ok' ? 'text-emerald-400' : 'text-amber-400',
      )}>
        {label}
      </span>
      {probe.probed_at && (
        <span className="text-[10px] text-ink-600 shrink-0">{fmtRelative(probe.probed_at)}</span>
      )}
    </div>
  )
}
