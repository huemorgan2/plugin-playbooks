/**
 * 009.001/phase04 — iframe auth bridge.
 *
 * The Shell posts `{type:'luna-auth', token}` into every plugin iframe on
 * load and whenever the session changes; an iframe that hits a 401 posts
 * `{type:'luna-request-auth'}` back to get a fresh one. The postMessage is
 * the source of truth; localStorage('luna.token') is only a same-origin
 * fallback so the app also works opened directly (dev, dojo).
 */

let token: string | null = null
let waiters: ((t: string) => void)[] = []

export function installAuthListener(): void {
  window.addEventListener('message', (e: MessageEvent) => {
    if (e.data && e.data.type === 'luna-auth' && typeof e.data.token === 'string') {
      token = e.data.token
      const w = waiters.splice(0)
      for (const resolve of w) resolve(e.data.token)
    }
  })
}

export function requestAuth(): void {
  try {
    window.parent?.postMessage({ type: 'luna-request-auth' }, '*')
  } catch {
    /* not in an iframe */
  }
}

function fallbackToken(): string | null {
  try {
    return localStorage.getItem('luna.token')
  } catch {
    return null
  }
}

export function getToken(): string | null {
  return token || fallbackToken()
}

/** Resolve the current token, asking the shell and waiting briefly if absent. */
export function getTokenAsync(timeoutMs = 3000): Promise<string> {
  const t = getToken()
  if (t) return Promise.resolve(t)
  requestAuth()
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      waiters = waiters.filter((w) => w !== onToken)
      resolve(fallbackToken() || '')
    }, timeoutMs)
    const onToken = (fresh: string) => {
      clearTimeout(timer)
      resolve(fresh)
    }
    waiters.push(onToken)
  })
}

/** A 401 means our token went stale — drop it and ask the shell for a new one. */
export function invalidateToken(): void {
  token = null
  requestAuth()
}
