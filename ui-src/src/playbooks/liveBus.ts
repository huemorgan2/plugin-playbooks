/**
 * 006.709 — tiny buffered bridge for live playbook events.
 *
 * `luna:playbook-open` / `luna:playbook-patch` CustomEvents are dispatched by
 * the Shell when the agent mutates a playbook. If the Playbooks section (or
 * the editor) isn't mounted yet — the user was on Chat when the agent started
 * building — a plain dispatch would be lost in the void. Events are buffered
 * here until the consumer mounts and declares itself ready, then replayed in
 * order.
 */

type Kind = 'open' | 'patch'

const buffers: Record<Kind, unknown[]> = { open: [], patch: [] }
const ready: Record<Kind, boolean> = { open: false, patch: false }

export function emitPlaybookEvent(kind: Kind, detail: unknown): void {
  if (ready[kind]) {
    window.dispatchEvent(new CustomEvent(`luna:playbook-${kind}`, { detail }))
  } else {
    buffers[kind].push(detail)
  }
}

/** Consumers call this right after addEventListener (and with `false` on
 * unmount). Flushing replays anything that arrived while unmounted. */
export function setPlaybookConsumerReady(kind: Kind, isReady: boolean): void {
  ready[kind] = isReady
  if (isReady) {
    const queued = buffers[kind].splice(0)
    for (const detail of queued) {
      window.dispatchEvent(new CustomEvent(`luna:playbook-${kind}`, { detail }))
    }
  }
}
