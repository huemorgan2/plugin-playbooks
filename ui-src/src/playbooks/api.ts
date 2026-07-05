import type { PlaybookSummary, PlaybookRunSummary, PlaybookRunDetail } from './types'
import { getToken, getTokenAsync, invalidateToken } from '../lib/auth'

async function doFetch(path: string, tok: string, opts?: RequestInit): Promise<Response> {
  return fetch(path, {
    ...opts,
    headers: {
      Authorization: `Bearer ${tok}`,
      'Content-Type': 'application/json',
      ...opts?.headers,
    },
  })
}

async function apiFetch<T>(path: string, opts?: RequestInit): Promise<T> {
  // The shell posts the token in after iframe load — wait for it rather than
  // firing an unauthenticated first request. One 401 → refresh-and-retry.
  let res = await doFetch(path, getToken() || (await getTokenAsync()), opts)
  if (res.status === 401) {
    invalidateToken()
    res = await doFetch(path, await getTokenAsync(), opts)
  }
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText)
    throw new Error(`${res.status}: ${text}`)
  }
  return res.json()
}

const BASE = '/api/p/plugin-playbooks'

export const playbooksApi = {
  list: (status: 'active' | 'archived' | 'all' = 'active') =>
    apiFetch<PlaybookSummary[]>(`${BASE}/playbooks?status=${status}`),

  get: (name: string) =>
    apiFetch<PlaybookSummary & { definition: any; inputs_schema: any }>(`${BASE}/playbooks/${name}`),

  create: (body: {
    name: string
    display_name?: string
    description?: string
    definition_yaml: string
    agent_autonomy?: string
  }) =>
    apiFetch<{ id: string; name: string; status: string }>(`${BASE}/playbooks`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  update: (name: string, definition_yaml: string, message = '') =>
    apiFetch<{ name: string; version: number }>(`${BASE}/playbooks/${name}`, {
      method: 'PUT',
      body: JSON.stringify({ definition_yaml, message }),
    }),

  archive: (name: string) =>
    apiFetch<{ name: string }>(`${BASE}/playbooks/${name}`, { method: 'DELETE' }),

  enable: (name: string) =>
    apiFetch<{ name: string }>(`${BASE}/playbooks/${name}/enable`, { method: 'POST' }),

  disable: (name: string) =>
    apiFetch<{ name: string }>(`${BASE}/playbooks/${name}/disable`, { method: 'POST' }),

  patch: (name: string, body: { enabled?: boolean; display_name?: string; description?: string }) =>
    apiFetch<{ name: string }>(`${BASE}/playbooks/${name}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),

  setAutonomy: (name: string, agent_autonomy: string) =>
    apiFetch<{ name: string }>(`${BASE}/playbooks/${name}/autonomy`, {
      method: 'PATCH',
      body: JSON.stringify({ agent_autonomy }),
    }),

  listRuns: (name: string) =>
    apiFetch<PlaybookRunSummary[]>(`${BASE}/playbooks/${name}/runs`),

  getRun: (runId: string) =>
    apiFetch<PlaybookRunDetail>(`${BASE}/playbooks/runs/${runId}`),

  startRun: (name: string, inputs: Record<string, any> = {}, trigger = 'manual') =>
    apiFetch<{ run_id: string; status: string }>(`${BASE}/playbooks/${name}/runs`, {
      method: 'POST',
      body: JSON.stringify({ inputs, trigger }),
    }),

  cancelRun: (runId: string) =>
    apiFetch<{ run_id: string }>(`${BASE}/playbooks/runs/${runId}/cancel`, { method: 'POST' }),

  // Versions
  listVersions: (name: string) =>
    apiFetch<{
      version: number
      title: string
      author: string
      created_at: string
      runs: number
      promoted_from: number | null
      current: boolean
    }[]>(`${BASE}/playbooks/${name}/versions`),

  promoteVersion: (name: string, version: number) =>
    apiFetch<{ name: string; version: number; promoted_from: number; status: string }>(
      `${BASE}/playbooks/${name}/promote`,
      { method: 'POST', body: JSON.stringify({ version }) },
    ),

  // Drafts
  createDraft: (name?: string) =>
    apiFetch<{ id: string; name: string; definition: any }>(`${BASE}/drafts`, {
      method: 'POST',
      body: JSON.stringify(name ? { name } : {}),
    }),

  getDraft: (draftId: string) =>
    apiFetch<{ id: string; name: string; definition: any }>(`${BASE}/drafts/${draftId}`),

  updateDraft: (draftId: string, body: { definition?: any; name?: string }) =>
    apiFetch<{ id: string; name: string }>(`${BASE}/drafts/${draftId}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  promoteDraft: (draftId: string) =>
    apiFetch<{ id: string; name: string; status: string }>(`${BASE}/drafts/${draftId}/promote`, {
      method: 'POST',
    }),

  deleteDraft: (draftId: string) =>
    apiFetch<{ id: string }>(`${BASE}/drafts/${draftId}`, { method: 'DELETE' }),
}
