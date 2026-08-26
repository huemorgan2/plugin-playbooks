// 0.13.0 (plans/002 phase 6): pure label logic for the list trust row and
// the Tests tab headlines. Plain words only — internal failure classes are
// translated before a human sees them.
import type { TrustSummary } from './types'

export type Tone = 'ok' | 'warn' | 'bad' | 'dim'

export interface TrustLabel {
  text: string
  tone: Tone
}

export function specsLabel(t?: TrustSummary): TrustLabel {
  const s = t?.specs
  if (!s || s.total === 0) return { text: 'untested', tone: 'warn' }
  if (s.failed > 0) return { text: `${s.failed} failing`, tone: 'bad' }
  return { text: `tests ${s.total}/${s.total}`, tone: 'ok' }
}

export function probesLabel(t?: TrustSummary): TrustLabel {
  const p = t?.probes
  if (!p || p.total === 0) return { text: 'unchecked', tone: 'warn' }
  if (p.failed > 0) {
    return { text: `${p.failed} broken tool${p.failed === 1 ? '' : 's'}`, tone: 'bad' }
  }
  return { text: 'tools ok', tone: 'ok' }
}

export function intentLabel(t?: TrustSummary): TrustLabel {
  if (t?.manifest_present) return { text: 'intent ✓', tone: 'ok' }
  return { text: 'no intent', tone: 'warn' }
}

// Failure classes are protocol values — render them as plain words.
const FAILURE_WORDS: Record<string, string> = {
  tool_missing: 'tool not installed',
  blocked: 'blocked by policy',
  credential_dead: 'credential dead',
  resource_gone: 'resource gone',
  permission: 'no permission',
  rate_limited: 'rate limited',
  unknown: 'failed',
}

export function failureWords(failureClass: string | null | undefined): string {
  if (!failureClass) return 'failed'
  return FAILURE_WORDS[failureClass] ?? failureClass.replace(/_/g, ' ')
}

// Tests tab headlines — the bottom line, not the category name.
export function specsHeadline(total: number, failed: number): TrustLabel {
  if (total === 0) return { text: 'No tests yet', tone: 'warn' }
  if (failed > 0) return { text: `${failed} of ${total} failing`, tone: 'bad' }
  return { text: `${total}/${total} passing`, tone: 'ok' }
}

export function probesHeadline(
  total: number, ok: number, failed: number,
): TrustLabel {
  if (total === 0) return { text: 'Nothing verified yet', tone: 'warn' }
  if (failed > 0) {
    return { text: `${failed} tool${failed === 1 ? '' : 's'} broken`, tone: 'bad' }
  }
  if (ok === 0) return { text: 'Nothing verified yet — no tool answers probes', tone: 'warn' }
  if (ok < total) {
    return { text: `${ok} of ${total} tools answered`, tone: 'ok' }
  }
  return { text: `All ${total} tool${total === 1 ? '' : 's'} answered`, tone: 'ok' }
}

export const TONE_TEXT: Record<Tone, string> = {
  ok: 'text-emerald-400',
  warn: 'text-amber-400',
  bad: 'text-rose-400',
  dim: 'text-ink-500',
}
