// 008.006: heartbeat-driven presence for the playbook list "Running" badge.
//
// A run is running while heartbeats keep arriving. `running = (now - lastBeat)
// < TTL`, so a missed `activity.completed` (crash) clears on its own once the
// beats stop. `activity.completed` clears instantly. Pure + side-effect-light
// so it can be unit-tested without the React tree.

export const ACTIVITY_TTL_MS = 8000

export type BeatMap = Map<string, number>

export interface ActivityEvent {
  event: 'activity.started' | 'activity.heartbeat' | 'activity.completed'
  kind?: string
  label?: string
  meta?: Record<string, unknown> | null
}

/** The key a playbook activity is tracked under (slug preferred, label fallback). */
export function activityKey(ev: ActivityEvent): string | null {
  return (ev.meta?.playbook_name as string | undefined) || ev.label || null
}

/**
 * Fold one activity event into the beat map. `started`/`heartbeat` stamp the
 * key with `now`; `completed` removes it. Non-playbook activities are ignored.
 * Mutates and returns `beats`.
 */
export function applyActivity(beats: BeatMap, ev: ActivityEvent, now: number): BeatMap {
  if (ev.kind && ev.kind !== 'playbook') return beats
  const key = activityKey(ev)
  if (!key) return beats
  if (ev.event === 'activity.completed') beats.delete(key)
  else beats.set(key, now)
  return beats
}

/**
 * The set of keys still running at `now`. Prunes (mutates) entries whose last
 * beat is older than `ttl` so the badge self-clears.
 */
export function runningNames(beats: BeatMap, now: number, ttl: number = ACTIVITY_TTL_MS): Set<string> {
  const live = new Set<string>()
  for (const [key, ts] of beats) {
    if (now - ts < ttl) live.add(key)
    else beats.delete(key)
  }
  return live
}
