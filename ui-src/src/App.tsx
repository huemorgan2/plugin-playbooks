/**
 * 009.001/phase04 — the Playbooks plugin UI, standalone in an iframe.
 *
 * Live events arrive from the Shell as `{type:'luna-plugin-event', event,
 * payload}` postMessages (E12 bridge). They map onto the internal liveBus
 * exactly like the old in-core Shell handlers did:
 *   playbook.open  → open
 *   playbook.patch → open + patch  (auto-follow the agent's change)
 *   navigate       → open           (navigate_to target)
 * The listener is installed BEFORE `luna-ui-ready` is posted, so the Shell's
 * buffered flush can't race the handler.
 */

import { useEffect, useState } from 'react'
import { PlaybooksSection } from './playbooks/PlaybooksSection'
import { emitPlaybookEvent } from './playbooks/liveBus'

export function App() {
  // Mount the section only after the bridge handshake is wired, so liveBus
  // consumer registration (inside PlaybooksSection) happens post-listener.
  const [wired, setWired] = useState(false)

  useEffect(() => {
    const onMsg = (e: MessageEvent) => {
      const d = e.data
      if (!d || d.type !== 'luna-plugin-event' || typeof d.event !== 'string') return
      const payload = d.payload || {}
      if (d.event === 'playbook.open') {
        emitPlaybookEvent('open', payload)
      } else if (d.event === 'playbook.patch') {
        emitPlaybookEvent('open', payload)
        emitPlaybookEvent('patch', payload)
      } else if (d.event === 'navigate') {
        if (payload.target) emitPlaybookEvent('open', { draft_id: payload.target })
      }
    }
    window.addEventListener('message', onMsg)
    setWired(true)
    try {
      window.parent?.postMessage({ type: 'luna-ui-ready' }, '*')
    } catch {
      /* not embedded */
    }
    return () => window.removeEventListener('message', onMsg)
  }, [])

  if (!wired) return null
  return <PlaybooksSection />
}
