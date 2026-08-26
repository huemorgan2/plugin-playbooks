/**
 * TestsTab (0.13.0, plans/002 phase 6) — the playbook's trust page.
 * Two sections, each eyebrow → bottom-line headline → one-line rows:
 *   TESTS — stored specs + last results, Run all.
 *   CONNECTIONS — cached tool probes, Check now.
 * Failure classes render as plain words (trust.ts), never protocol codes.
 */
import { useCallback, useEffect, useState } from 'react'
import {
  Loader2, Play, ChevronDown, ChevronRight, RefreshCw,
} from 'lucide-react'
import { cn } from '../lib/cn'
import { playbooksApi } from './api'
import type { SpecEntry, ProbeEntry } from './types'
import { specsHeadline, probesHeadline, failureWords, TONE_TEXT } from './trust'

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

export function TestsTab({ name }: { name: string }) {
  const [specs, setSpecs] = useState<SpecEntry[] | null>(null)
  const [probes, setProbes] = useState<ProbeEntry[] | null>(null)
  const [runningSpecs, setRunningSpecs] = useState(false)
  const [runningPreflight, setRunningPreflight] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(() => {
    playbooksApi.getSpecs(name).then((r) => setSpecs(r.specs)).catch(() => setSpecs([]))
    playbooksApi.getProbes(name).then((r) => setProbes(r.probes)).catch(() => setProbes([]))
  }, [name])

  useEffect(() => { refresh() }, [refresh])

  const runAllSpecs = async () => {
    if (runningSpecs) return
    setRunningSpecs(true)
    setError(null)
    try {
      await playbooksApi.runSpecs(name)
      refresh()
    } catch (e: any) {
      setError(e.message)
    } finally {
      setRunningSpecs(false)
    }
  }

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

  if (specs === null || probes === null) {
    return (
      <div className="flex items-center justify-center h-full text-ink-400">
        <Loader2 className="w-5 h-5 animate-spin" />
      </div>
    )
  }

  const specFailed = specs.filter(
    (s) => s.last_result && s.last_result.passed === false,
  ).length
  const specVerdict = specsHeadline(specs.length, specFailed)
  const probeFailed = probes.filter((p) => p.status === 'failed').length
  const probeOk = probes.filter((p) => p.status === 'ok').length
  const probeVerdict = probesHeadline(probes.length, probeOk, probeFailed)

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-2xl mx-auto py-4 px-4 space-y-8">
        {error && (
          <p className="text-xs text-rose-400">{error}</p>
        )}

        {/* TESTS */}
        <section>
          <div className="flex items-center justify-between">
            <div className="text-[11px] uppercase tracking-[0.16em] text-ink-500">Tests</div>
            <button
              onClick={runAllSpecs}
              disabled={runningSpecs || specs.length === 0}
              className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-[11px] font-medium text-luna-400 hover:bg-luna-600/20 disabled:opacity-40 transition"
              data-testid="specs-run-all"
            >
              {runningSpecs ? <Loader2 className="w-3 h-3 animate-spin" /> : <Play className="w-3 h-3" />}
              Run all
            </button>
          </div>
          <div
            className={cn('text-xl font-semibold mt-1', TONE_TEXT[specVerdict.tone])}
            data-testid="specs-headline"
          >
            {specVerdict.text}
          </div>
          <p className="text-xs text-ink-500 mt-0.5">
            {specs.length === 0
              ? 'Ask Luna to write tests from a good run, or from what this playbook should do.'
              : 'Each test dry-runs the playbook and checks what it should do.'}
          </p>
          {specs.length > 0 && (
            <div className="mt-3 rounded-xl border border-white/5 overflow-hidden">
              {specs.map((s) => <SpecRow key={s.name} spec={s} />)}
            </div>
          )}
        </section>

        {/* CONNECTIONS */}
        <section>
          <div className="flex items-center justify-between">
            <div className="text-[11px] uppercase tracking-[0.16em] text-ink-500">Connections</div>
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

function SpecRow({ spec }: { spec: SpecEntry }) {
  const [open, setOpen] = useState(false)
  const last = spec.last_result
  const tone = !last ? 'warn' as const : last.passed === false ? 'bad' as const : 'ok' as const
  const failures: string[] = Array.isArray(last?.failures) ? last!.failures : []
  return (
    <div className="border-b border-white/5 last:border-b-0" data-testid="spec-row">
      <button
        onClick={() => setOpen((v) => !v)}
        className={cn(
          'w-full flex items-center gap-2.5 px-3 py-2.5 text-left transition',
          open ? 'bg-white/[.03]' : 'hover:bg-white/[.02]',
        )}
      >
        {open
          ? <ChevronDown className="w-3 h-3 text-ink-600 shrink-0" />
          : <ChevronRight className="w-3 h-3 text-ink-600 shrink-0" />}
        <Dot tone={tone} />
        <span className="text-xs text-ink-200 truncate flex-1">{spec.name}</span>
        {tone === 'bad' && <span className="text-[10px] text-rose-400 shrink-0">failing</span>}
        {tone === 'warn' && <span className="text-[10px] text-amber-400 shrink-0">never ran</span>}
        {spec.last_run_at && (
          <span className="text-[10px] text-ink-600 shrink-0">{fmtRelative(spec.last_run_at)}</span>
        )}
      </button>
      {open && (
        <div className="px-3 pb-3 pl-9 space-y-2">
          {failures.length > 0 && (
            <div>
              <div className="text-[10px] text-ink-500 mb-0.5">What failed</div>
              <ul className="space-y-0.5">
                {failures.map((f, i) => (
                  <li key={i} className="text-[11px] text-rose-300">{f}</li>
                ))}
              </ul>
            </div>
          )}
          <div>
            <div className="text-[10px] text-ink-500 mb-0.5">Test definition</div>
            <pre className="text-[10px] text-ink-300 font-mono whitespace-pre-wrap bg-ink-900/60 rounded p-2 max-h-64 overflow-auto">
              {JSON.stringify(spec.spec, null, 2)}
            </pre>
          </div>
        </div>
      )}
    </div>
  )
}

function ProbeRow({ probe }: { probe: ProbeEntry }) {
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
