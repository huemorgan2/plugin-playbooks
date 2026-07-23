/**
 * plans/001 — how the list says when a playbook last ran and how often it runs.
 * Plain words, no units the owner has to decode: "ran 2h ago", "3.4/day".
 */

import type { PlaybookSummary } from './types'

/** "ran 2h ago" — or "never run" when there is no run at all. */
export function lastRunLabel(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return 'never run'
  const t = new Date(iso).getTime()
  if (!Number.isFinite(t)) return 'never run'
  const mins = Math.floor((now - t) / 60000)
  if (mins < 1) return 'ran just now'
  if (mins < 60) return `ran ${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `ran ${hours}h ago`
  const days = Math.floor(hours / 24)
  if (days < 30) return `ran ${days}d ago`
  const months = Math.floor(days / 30)
  return months < 12 ? `ran ${months}mo ago` : `ran ${Math.floor(days / 365)}y ago`
}

/**
 * "3.4/day" over the last 30 days. Empty when the playbook never ran — the
 * last-run label already says that, and "0.0/day" next to it reads as noise.
 * A playbook that ran but rounds to zero reads "<0.1/day", never "0.0/day".
 */
export function rateLabel(pb: Pick<PlaybookSummary, 'runs_per_day' | 'runs_window'>): string {
  const runs = pb.runs_window ?? 0
  if (!runs) return ''
  const rate = pb.runs_per_day ?? 0
  return rate < 0.1 ? '<0.1/day' : `${rate.toFixed(1)}/day`
}
