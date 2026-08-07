/**
 * 009.001/phase04 — the plugin UI's own live-event feed.
 *
 * `activity.*` presence beats (the "Running" badge) and `playbook.*` run
 * progress ride the global `/api/events` SSE. The iframe is same-origin with
 * the API, so it opens its own authed stream — no shell relay needed for
 * non-envelope topics.
 *
 * ONE underlying connection, fanned out to all subscribers (browsers cap
 * HTTP/1.1 at ~6 sockets per origin; the shell already holds several).
 * Opens on the first subscriber, closes when the last unsubscribes.
 * Reconnects with backoff; 401s trigger a token refresh via the auth bridge.
 */

import { fetchEventSource } from '@microsoft/fetch-event-source'
import { getTokenAsync, invalidateToken } from './auth'

export interface ActivityInfo {
  event: 'activity.started' | 'activity.heartbeat' | 'activity.completed'
  kind?: string
  label?: string
  meta?: Record<string, unknown> | null
}

export interface PlaybookRunEvent {
  event:
    | 'playbook.run.started'
    | 'playbook.run.completed'
    | 'playbook.step.started'
    | 'playbook.step.completed'
    | 'playbook.step.failed'
    | 'playbook.step.waiting'
  run_id: string
  playbook_name?: string
  step_id?: string
  status?: string
  [key: string]: unknown
}

type Handlers = {
  onActivity?: (info: ActivityInfo) => void
  onPlaybook?: (evt: PlaybookRunEvent) => void
}

const subs = new Set<Handlers>()
let ctrl: AbortController | null = null

function dispatch(event: string, data: Record<string, unknown>) {
  for (const h of subs) {
    try {
      if (event.startsWith('activity.')) {
        h.onActivity?.({ event: event as ActivityInfo['event'], ...data })
      } else if (event.startsWith('playbook.')) {
        h.onPlaybook?.({ event, ...data } as PlaybookRunEvent)
      }
    } catch {
      /* one bad handler must not stall the fan-out */
    }
  }
}

async function run(signal: AbortSignal) {
  // Endless reconnect loop — fetchEventSource's own retry gives up on
  // fatal responses; we own the backoff so a server restart heals.
  while (!signal.aborted) {
    const tok = await getTokenAsync()
    try {
      await fetchEventSource('/api/events?topics=activity.*,playbook.*', {
        signal,
        headers: { Authorization: `Bearer ${tok}` },
        openWhenHidden: true,
        onmessage: (msg) => {
          if (!msg.event) return
          if (!msg.event.startsWith('activity.') && !msg.event.startsWith('playbook.')) return
          try {
            dispatch(msg.event, JSON.parse(msg.data || '{}'))
          } catch {
            /* malformed frame — skip */
          }
        },
        onopen: async (res) => {
          if (res.status === 401) invalidateToken()
          if (!res.ok) throw new Error(`events: ${res.status}`)
        },
        onerror: (err) => {
          throw err // exit fetchEventSource; outer loop backs off + retries
        },
      })
    } catch {
      /* fall through to backoff */
    }
    if (signal.aborted) break
    await new Promise((r) => setTimeout(r, 2000))
  }
}

function subscribe(h: Handlers): () => void {
  subs.add(h)
  if (!ctrl) {
    ctrl = new AbortController()
    void run(ctrl.signal)
  }
  return () => {
    subs.delete(h)
    if (subs.size === 0) {
      ctrl?.abort()
      ctrl = null
    }
  }
}

export function subscribeActivityEvents(onActivity: (info: ActivityInfo) => void): () => void {
  return subscribe({ onActivity })
}

/** Live run progress: run started/completed + per-step transitions. */
export function subscribePlaybookEvents(onPlaybook: (evt: PlaybookRunEvent) => void): () => void {
  return subscribe({ onPlaybook })
}
