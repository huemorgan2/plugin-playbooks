import { useEffect, useState } from 'react'
import { identityApi, type AgentIdentity } from './api'

// Module-level cache: one fetch per iframe load, shared by every component.
const FALLBACK: AgentIdentity = { name: 'Luna', emoji: '' }
let cached: AgentIdentity | null = null
let inflight: Promise<AgentIdentity> | null = null

function load(): Promise<AgentIdentity> {
  inflight ??= identityApi
    .get()
    .then((id) => (id?.name ? id : FALLBACK))
    .catch(() => FALLBACK)
  return inflight
}

/** The agent's display name ("Luna" until loaded / if unavailable). */
export function useAgentName(): string {
  const [identity, setIdentity] = useState<AgentIdentity>(cached ?? FALLBACK)
  useEffect(() => {
    if (cached) return
    let alive = true
    load().then((id) => {
      cached = id
      if (alive) setIdentity(id)
    })
    return () => {
      alive = false
    }
  }, [])
  return identity.name
}

/** Test hook: reset the module cache. */
export function _resetAgentIdentityCache() {
  cached = null
  inflight = null
}
