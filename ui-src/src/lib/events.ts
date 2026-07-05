/**
 * 009.001/phase04 — the plugin UI's own live-event feed.
 *
 * `activity.*` presence beats (the "Running" badge) ride the global
 * `/api/events` SSE. The iframe is same-origin with the API, so it opens its
 * own authed stream — no shell relay needed for non-envelope topics.
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

export function subscribeActivityEvents(onActivity: (info: ActivityInfo) => void): () => void {
  const ctrl = new AbortController()

  const run = async () => {
    // Endless reconnect loop — fetchEventSource's own retry gives up on
    // fatal responses; we own the backoff so a server restart heals.
    while (!ctrl.signal.aborted) {
      const tok = await getTokenAsync()
      try {
        await fetchEventSource('/api/events?topics=activity.*', {
          signal: ctrl.signal,
          headers: { Authorization: `Bearer ${tok}` },
          openWhenHidden: true,
          onmessage: (msg) => {
            if (!msg.event || !msg.event.startsWith('activity.')) return
            try {
              const data = JSON.parse(msg.data || '{}')
              onActivity({ event: msg.event as ActivityInfo['event'], ...data })
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
      if (ctrl.signal.aborted) break
      await new Promise((r) => setTimeout(r, 2000))
    }
  }

  void run()
  return () => ctrl.abort()
}
