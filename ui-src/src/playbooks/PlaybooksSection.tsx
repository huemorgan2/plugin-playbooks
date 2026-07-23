/**
 * PlaybooksSection — top-level view for the Playbooks sidebar item.
 * Shows a list of playbooks + drafts; clicking one opens the canvas editor.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Plus, Loader2,
  Workflow,
  Archive, ArchiveRestore,
} from 'lucide-react'
import { cn } from '../lib/cn'
import { subscribeActivityEvents } from '../lib/events'
import { playbooksApi } from './api'
import type { PlaybookSummary } from './types'
import { PlaybookEditor } from './PlaybookEditor'
import { setPlaybookConsumerReady } from './liveBus'
import { applyActivity, runningNames as computeRunning } from './activityPresence'
import { lastRunLabel, rateLabel } from './runStats'

type SelectedItem = { kind: 'playbook'; name: string } | { kind: 'draft'; id: string }
type Tab = 'active' | 'archived'

export function PlaybooksSection({ onNavigate: _onNavigate }: { onNavigate?: (section: string) => void }) {
  const [playbooks, setPlaybooks] = useState<PlaybookSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState<SelectedItem | null>(null)
  const [creating, setCreating] = useState(false)
  const [tab, setTab] = useState<Tab>('active')
  // 008.006: playbook name → last heartbeat ms. Derived `runningNames` drives
  // the badge; a 2s sweep drops stale beats so it self-clears within the TTL.
  const lastBeat = useRef<Map<string, number>>(new Map())
  const [runningNames, setRunningNames] = useState<Set<string>>(new Set())

  useEffect(() => {
    const recompute = () => {
      const live = computeRunning(lastBeat.current, Date.now())
      setRunningNames((prev) => {
        if (prev.size === live.size && [...live].every((n) => prev.has(n))) return prev
        return live
      })
    }
    const unsubscribe = subscribeActivityEvents((info) => {
      applyActivity(lastBeat.current, info, Date.now())
      recompute()
    })
    const sweep = setInterval(recompute, 2000)
    return () => {
      unsubscribe()
      clearInterval(sweep)
    }
  }, [])

  const refresh = useCallback(() => {
    setLoading(true)
    playbooksApi
      .list(tab)
      .then(setPlaybooks)
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [tab])

  useEffect(() => { refresh() }, [refresh])

  // 006.709: the agent can open a playbook for the user (ui.playbook.open /
  // navigate_to target / auto-follow on patches). detail.draft_id can be a
  // draft UUID or a live playbook name. Redundant opens are no-ops.
  useEffect(() => {
    const onOpen = (e: Event) => {
      const d = (e as CustomEvent).detail as { draft_id?: string; id?: string; name?: string }
      const target = d.draft_id || d.id || d.name
      if (!target) return
      const isUuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(target)
      setSelected((prev) => {
        if (prev) {
          if (prev.kind === 'draft' && prev.id === target) return prev
          if (prev.kind === 'playbook' && (prev.name === target || prev.name === d.name)) return prev
        }
        return isUuid ? { kind: 'draft', id: target } : { kind: 'playbook', name: target }
      })
      refresh() // a new draft should appear in the list too
    }
    window.addEventListener('luna:playbook-open', onOpen)
    setPlaybookConsumerReady('open', true) // replay opens missed while unmounted
    return () => {
      setPlaybookConsumerReady('open', false)
      window.removeEventListener('luna:playbook-open', onOpen)
    }
  }, [refresh])

  const createNewPlaybook = async () => {
    if (creating) return
    setCreating(true)
    try {
      const draft = await playbooksApi.createDraft()
      setSelected({ kind: 'draft', id: draft.id })
    } catch {
    } finally {
      setCreating(false)
    }
  }

  const toggleStatus = async (pb: PlaybookSummary) => {
    try {
      const newStatus = pb.status === 'enabled' ? 'disabled' : 'enabled'
      await playbooksApi.patch(pb.name, { enabled: newStatus === 'enabled' })
      setPlaybooks((ps) =>
        ps.map((p) => (p.id === pb.id ? { ...p, status: newStatus } : p)),
      )
    } catch {}
  }

  const archivePlaybook = async (name: string) => {
    try {
      await playbooksApi.archive(name)
      setPlaybooks((ps) => ps.filter((p) => p.name !== name))
    } catch {}
  }

  const unarchivePlaybook = async (name: string) => {
    try {
      await playbooksApi.enable(name)
      setPlaybooks((ps) => ps.filter((p) => p.name !== name))
    } catch {}
  }

  if (selected) {
    return (
      <PlaybookEditor
        {...(selected.kind === 'playbook' ? { name: selected.name } : { draftId: selected.id })}
        onBack={() => {
          setSelected(null)
          refresh()
        }}
      />
    )
  }

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="flex items-center justify-between px-6 py-4 border-b border-white/5 shrink-0">
        <div>
          <h2 className="text-lg font-semibold text-ink-50">Playbooks</h2>
          <p className="text-xs text-ink-500 mt-0.5">
            Multi-step workflows Luna builds and runs for you
          </p>
        </div>
        {tab === 'active' && (
          <button
            onClick={createNewPlaybook}
            disabled={creating}
            className="inline-flex items-center gap-2 rounded-lg bg-luna-600 hover:bg-luna-500 disabled:opacity-50 transition text-white text-sm font-medium py-2 px-4"
          >
            {creating ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />}
            New playbook
          </button>
        )}
      </div>

      {/* Tabs */}
      <div className="flex gap-1 px-6 pt-3 pb-1 shrink-0">
        {(['active', 'archived'] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={cn(
              'px-3 py-1.5 rounded-md text-xs font-medium transition',
              tab === t
                ? 'bg-luna-600/20 text-luna-400'
                : 'text-ink-500 hover:text-ink-300 hover:bg-white/5',
            )}
          >
            {t === 'active' ? 'Active' : 'Archived'}
          </button>
        ))}
      </div>

      {/* List */}
      {loading ? (
        <div className="flex items-center justify-center flex-1 text-ink-400 gap-2">
          <Loader2 className="w-5 h-5 animate-spin" />
          Loading…
        </div>
      ) : playbooks.length === 0 ? (
        <div className="flex-1 flex flex-col items-center justify-center text-ink-500 gap-3 px-8">
          <Workflow className="w-12 h-12 text-ink-600" />
          <p className="text-sm text-center">
            {tab === 'active'
              ? 'No playbooks yet. Click "New playbook" to create one, or ask Luna in chat to build one for you.'
              : 'No archived playbooks.'}
          </p>
        </div>
      ) : (
        <div className="flex-1 overflow-y-auto">
          {playbooks.map((pb) => (
            <div
              key={pb.id}
              className="flex items-center gap-3 px-6 py-3.5 border-b border-white/5 hover:bg-white/[.02] transition cursor-pointer group"
              onClick={() => setSelected({ kind: 'playbook', name: pb.name })}
            >
              <div className="flex-1 min-w-0">
                <span className="font-medium text-ink-100 truncate">
                  {pb.display_name || pb.name}
                </span>
                {runningNames.has(pb.name) && (
                  <span
                    className="ml-2 inline-flex items-center gap-1.5 align-middle rounded-full bg-emerald-500/15 text-emerald-400 text-[10px] font-medium px-2 py-0.5"
                    data-testid="playbook-running-badge"
                  >
                    <span className="relative flex h-1.5 w-1.5">
                      <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
                      <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-emerald-400" />
                    </span>
                    Running
                  </span>
                )}
                {pb.description && (
                  <p className="text-xs text-ink-500 truncate mt-0.5">{pb.description}</p>
                )}
                <div className="flex items-center gap-3 mt-1 text-[11px] text-ink-600">
                  <span>v{pb.version}</span>
                  <span className="capitalize">{pb.agent_autonomy.replace(/_/g, ' ')}</span>
                  <span
                    data-testid="playbook-last-run"
                    title={pb.last_run_at ? new Date(pb.last_run_at).toLocaleString() : 'No runs yet'}
                  >
                    {lastRunLabel(pb.last_run_at)}
                  </span>
                  {rateLabel(pb) && (
                    <span data-testid="playbook-rate" title="Average runs per day over the last 30 days">
                      {rateLabel(pb)}
                    </span>
                  )}
                </div>
              </div>

              <div className="flex items-center gap-3 shrink-0">
                {tab === 'active' ? (
                  <>
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        archivePlaybook(pb.name)
                      }}
                      className="p-1.5 rounded text-ink-600 opacity-0 group-hover:opacity-100 hover:text-amber-400 hover:bg-white/10 transition"
                      title="Archive"
                    >
                      <Archive className="w-4 h-4" />
                    </button>
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        toggleStatus(pb)
                      }}
                      className={cn(
                        'relative w-10 h-5 rounded-full transition-colors',
                        pb.status === 'enabled' ? 'bg-emerald-600' : 'bg-ink-700',
                      )}
                      title={pb.status === 'enabled' ? 'Disable' : 'Enable'}
                    >
                      <div className={cn(
                        'absolute top-0.5 w-4 h-4 rounded-full bg-white transition-transform',
                        pb.status === 'enabled' ? 'left-[22px]' : 'left-0.5',
                      )} />
                    </button>
                  </>
                ) : (
                  <button
                    onClick={(e) => {
                      e.stopPropagation()
                      unarchivePlaybook(pb.name)
                    }}
                    className="p-1.5 rounded text-ink-400 hover:text-emerald-400 hover:bg-white/10 transition"
                    title="Unarchive"
                  >
                    <ArchiveRestore className="w-4 h-4" />
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
