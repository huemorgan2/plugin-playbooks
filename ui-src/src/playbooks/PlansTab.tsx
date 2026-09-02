/**
 * PlansTab (plans/016 phase 3, reworked in plans/022) — the owner's audit
 * trail of playbook changes plus the autonomy switch. A plan is one row of
 * readable text; publishes stamp code-generated outcome facts onto it.
 *
 * plans/022: the list is a table (title / status / playbooks / dates), the
 * status is owner-editable in place (setting a done plan back to proposed is
 * THE reopen switch — agent tools refuse done plans), rows open a full-page
 * reader, and plans can be deleted.
 */
import { useCallback, useEffect, useState } from 'react'
import { ArrowLeft, Loader2, Trash2 } from 'lucide-react'
import { cn } from '../lib/cn'
import { playbooksApi, type PlanBrief, type PlanDetail } from './api'
import { timeAgo } from './editorBits'

export const PLAN_STATUSES = ['proposed', 'approved', 'rejected', 'done']

// Status controls recolor border + text only, never the fill (ux_guidelines).
const PILL: Record<string, string> = {
  proposed: 'border-amber-500/40 text-amber-300',
  approved: 'border-emerald-500/40 text-emerald-300',
  rejected: 'border-rose-500/40 text-rose-300',
  done: 'border-white/15 text-ink-400',
}

/** The owner's status control — a pill that is secretly a <select>. */
export function PlanStatusSelect({ value, onChange }: {
  value: string
  onChange: (status: string) => void
}) {
  return (
    <select
      value={value}
      onClick={(e) => e.stopPropagation()}
      onChange={(e) => { e.stopPropagation(); onChange(e.target.value) }}
      className={cn(
        'appearance-none rounded-full border bg-transparent px-2.5 py-0.5',
        'text-[11px] font-medium capitalize cursor-pointer outline-none',
        'hover:bg-white/[.04] transition',
        PILL[value] ?? 'border-white/15 text-ink-400',
      )}
      data-testid="plan-status-select"
    >
      {PLAN_STATUSES.map((s) => (
        <option key={s} value={s} className="bg-ink-900 text-ink-100">{s}</option>
      ))}
    </select>
  )
}

// Plans are agent-written markdown-ish text. Render just **bold** and
// `code` inline — anything heavier stays plain (it's a note, not a doc).
export function renderInline(text: string): React.ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).map((part, i) => {
    if (part.startsWith('**') && part.endsWith('**')) {
      return <strong key={i} className="font-semibold text-ink-100">{part.slice(2, -2)}</strong>
    }
    if (part.startsWith('`') && part.endsWith('`') && part.length > 2) {
      return <code key={i} className="text-[.92em] text-luna-200">{part.slice(1, -1)}</code>
    }
    return part
  })
}

/** One short sentence per code-stamped fact — never a JSON dump. */
export function factLine(f: Record<string, any>): string {
  const verb =
    f.action === 'restore' ? 'Restored' :
    f.action === 'rollback' ? 'Rolled back' : 'Published'
  const vers = f.old_live_version != null
    ? `v${f.old_live_version} → v${f.new_live_version}`
    : `v${f.new_live_version}`
  const who = f.actor === 'owner' ? 'by you' : `by ${f.actor || 'the agent'}`
  let line = `${verb} ${f.playbook} ${vers} ${who}`
  if (f.test_run_forced) line += ' — without a fresh test run (your call)'
  return line
}

/** Compact table date: "Sep 2", with the year when it isn't this year. */
function fmtDate(iso: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (isNaN(d.getTime())) return '—'
  const opts: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric' }
  if (d.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric'
  return d.toLocaleDateString('en-US', opts)
}

/** Trash button with an in-place two-click confirm (no browser dialogs). */
function DeleteButton({ armed, onArm, onConfirm, testId }: {
  armed: boolean
  onArm: () => void
  onConfirm: () => void
  testId: string
}) {
  return armed ? (
    <button
      onClick={(e) => { e.stopPropagation(); onConfirm() }}
      className="text-[11px] font-medium text-rose-300 border border-rose-500/40 rounded-full px-2 py-0.5 hover:bg-rose-500/10 transition whitespace-nowrap"
      data-testid={`${testId}-confirm`}
    >
      Delete?
    </button>
  ) : (
    <button
      onClick={(e) => { e.stopPropagation(); onArm() }}
      className="p-1 text-ink-600 hover:text-rose-300 transition"
      title="Delete this plan"
      data-testid={testId}
    >
      <Trash2 className="w-3.5 h-3.5" />
    </button>
  )
}

/**
 * Full-page plan reader: large readable type, the status control, the
 * code-stamped facts and execution summary, delete, and a back link.
 */
function PlanReader({ planId, agentName, onBack, onStatus, onDelete }: {
  planId: string
  agentName: string
  onBack: () => void
  onStatus: (planId: string, status: string) => void
  onDelete: (planId: string) => void
}) {
  const [detail, setDetail] = useState<PlanDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [armDelete, setArmDelete] = useState(false)

  useEffect(() => {
    let cancelled = false
    playbooksApi.getPlan(planId)
      .then((d) => { if (!cancelled) setDetail(d) })
      .catch((e) => { if (!cancelled) setError(e.message) })
    return () => { cancelled = true }
  }, [planId])

  const setStatus = (status: string) => {
    setDetail((d) => (d ? { ...d, status } : d))
    onStatus(planId, status)
  }

  return (
    <div className="px-6 py-5 max-w-3xl" data-testid="plan-reader">
      <button
        onClick={onBack}
        className="flex items-center gap-1.5 text-xs text-ink-500 hover:text-ink-200 transition"
        data-testid="plan-back"
      >
        <ArrowLeft className="w-3.5 h-3.5" /> All plans
      </button>
      {error ? (
        <p className="text-sm text-rose-400 mt-4">{error}</p>
      ) : !detail ? (
        <div className="flex items-center justify-center py-10 text-ink-500">
          <Loader2 className="w-4 h-4 animate-spin" />
        </div>
      ) : (
        <>
          <h1 className="text-2xl font-semibold text-ink-50 mt-4 leading-snug">
            {detail.title}
          </h1>
          <div className="flex flex-wrap items-center gap-3 mt-3 text-xs text-ink-500">
            <PlanStatusSelect value={detail.status} onChange={setStatus} />
            {detail.playbook_refs.length > 0 && (
              <span data-testid="plan-refs">{detail.playbook_refs.join(', ')}</span>
            )}
            <span title={detail.created_at ?? ''}>
              Created {fmtDate(detail.created_at)}
            </span>
            <span title={detail.updated_at ?? ''}>
              Updated {detail.updated_at ? timeAgo(detail.updated_at) : '—'}
            </span>
            <span className="ml-auto">
              <DeleteButton
                armed={armDelete}
                onArm={() => setArmDelete(true)}
                onConfirm={() => onDelete(planId)}
                testId="plan-delete"
              />
            </span>
          </div>
          {detail.status === 'done' && (
            <p
              className="text-xs text-ink-500 border border-white/10 rounded-md px-3 py-2 mt-4"
              data-testid="plan-locked-note"
            >
              This plan is done — locked for {agentName}. Set its status back
              to “proposed” to reopen it.
            </p>
          )}
          <div
            className="text-base leading-relaxed text-ink-200 whitespace-pre-wrap mt-6"
            data-testid="plan-body"
          >
            {renderInline(detail.body)}
          </div>
          {detail.status === 'rejected' && detail.rejection_note && (
            <p className="text-sm text-rose-300 mt-5" data-testid="plan-rejection">
              Rejected: {detail.rejection_note}
            </p>
          )}
          {(detail.outcome_facts?.length ?? 0) > 0 && (
            <div className="mt-6 space-y-1.5" data-testid="plan-facts">
              <p className="text-[11px] uppercase tracking-[0.16em] text-ink-500 mb-2">
                Publishes
              </p>
              {detail.outcome_facts.map((f, i) => (
                <p key={i} className="text-sm text-ink-400">
                  {factLine(f)}
                  {f.at && <span className="text-ink-600"> · {timeAgo(f.at)}</span>}
                </p>
              ))}
            </div>
          )}
          {detail.execution_summary && (
            <div className="mt-6" data-testid="plan-summary">
              <p className="text-[11px] uppercase tracking-[0.16em] text-ink-500 mb-2">
                How it went
              </p>
              <p className="text-base leading-relaxed text-ink-300 whitespace-pre-wrap">
                {renderInline(detail.execution_summary)}
              </p>
            </div>
          )}
        </>
      )}
    </div>
  )
}

/**
 * The plans surface — reused by the section-level PlansTab (all plans) and
 * the editor's per-playbook Plans tab (plans/021: pass `playbook` to narrow
 * to plans referencing it). A table of rows; clicking one opens the reader.
 */
export function PlansList({ agentName, playbook }: { agentName: string; playbook?: string }) {
  const [plans, setPlans] = useState<PlanBrief[] | null>(null)
  const [openId, setOpenId] = useState<string | null>(null)
  const [armedDelete, setArmedDelete] = useState<string | null>(null)

  useEffect(() => {
    playbooksApi.listPlans(undefined, playbook)
      .then((r) => setPlans(r.plans))
      .catch(() => setPlans([]))
  }, [playbook])

  const setStatus = useCallback((planId: string, status: string) => {
    // optimistic; a failed PATCH re-syncs from the server
    setPlans((ps) => ps?.map((p) => (p.plan_id === planId ? { ...p, status } : p)) ?? ps)
    playbooksApi.patchPlan(planId, status).catch(() => {
      playbooksApi.listPlans(undefined, playbook)
        .then((r) => setPlans(r.plans)).catch(() => {})
    })
  }, [playbook])

  const remove = useCallback((planId: string) => {
    playbooksApi.deletePlan(planId)
      .then(() => {
        setPlans((ps) => ps?.filter((p) => p.plan_id !== planId) ?? ps)
        setOpenId((cur) => (cur === planId ? null : cur))
      })
      .catch(() => {})
      .finally(() => setArmedDelete(null))
  }, [])

  if (openId) {
    return (
      <PlanReader
        planId={openId}
        agentName={agentName}
        onBack={() => setOpenId(null)}
        onStatus={setStatus}
        onDelete={remove}
      />
    )
  }

  const awaiting = (plans ?? []).filter((p) => p.status === 'proposed' || p.status === 'approved').length

  return (
    <>
      <div className="px-6 pt-4 pb-1">
        <div className="text-[11px] uppercase tracking-[0.16em] text-ink-500 mb-1">Plans</div>
        <p className="text-base font-semibold text-ink-50" data-testid="plans-headline">
          {plans === null
            ? 'Loading…'
            : awaiting > 0
              ? `${awaiting} awaiting publish`
              : plans.length > 0
                ? 'No changes planned'
                : 'No plans yet'}
        </p>
        <p className="text-xs text-ink-500 mt-0.5 mb-2">
          Every playbook change starts with a plan you can read — publishes are
          stamped onto it. A done plan is locked for the agent until you set it
          back to proposed.
        </p>
      </div>
      {plans === null ? (
        <div className="flex items-center justify-center py-8 text-ink-500">
          <Loader2 className="w-4 h-4 animate-spin" />
        </div>
      ) : plans.length === 0 ? (
        <p className="text-sm text-ink-500 px-6 py-6">
          Ask {agentName} to change {playbook ? 'this playbook' : 'a playbook'} and
          its plan will show up here.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm" data-testid="plans-table">
            <thead>
              <tr className="text-left text-[10px] uppercase tracking-[0.14em] text-ink-500 border-b border-white/10">
                <th className="font-medium pl-6 pr-3 py-2">Plan</th>
                <th className="font-medium px-3 py-2">Status</th>
                <th className="font-medium px-3 py-2">Playbooks</th>
                <th className="font-medium px-3 py-2 whitespace-nowrap">Created</th>
                <th className="font-medium px-3 py-2 whitespace-nowrap">Updated</th>
                <th className="w-10 pr-4" />
              </tr>
            </thead>
            <tbody>
              {plans.map((p) => (
                <tr
                  key={p.plan_id}
                  onClick={() => setOpenId(p.plan_id)}
                  className="border-b border-white/5 hover:bg-white/[.02] cursor-pointer transition"
                  data-testid={`plan-row-${p.plan_id}`}
                >
                  <td className="pl-6 pr-3 py-2.5 font-medium text-ink-100 max-w-[22rem]">
                    <span className="block truncate">{p.title}</span>
                  </td>
                  <td className="px-3 py-2.5">
                    <PlanStatusSelect
                      value={p.status}
                      onChange={(s) => setStatus(p.plan_id, s)}
                    />
                  </td>
                  <td className="px-3 py-2.5 text-xs text-ink-400 whitespace-nowrap">
                    {p.playbook_refs.length > 0 ? p.playbook_refs.join(', ') : '—'}
                  </td>
                  <td
                    className="px-3 py-2.5 text-xs text-ink-500 whitespace-nowrap"
                    title={p.created_at ?? ''}
                  >
                    {fmtDate(p.created_at)}
                  </td>
                  <td
                    className="px-3 py-2.5 text-xs text-ink-500 whitespace-nowrap"
                    title={p.updated_at ?? ''}
                  >
                    {fmtDate(p.updated_at)}
                  </td>
                  <td className="pr-4 py-2.5 text-right">
                    <DeleteButton
                      armed={armedDelete === p.plan_id}
                      onArm={() => setArmedDelete(p.plan_id)}
                      onConfirm={() => remove(p.plan_id)}
                      testId={`plan-delete-${p.plan_id}`}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

export function PlansTab({ agentName }: { agentName: string }) {
  const [fullPower, setFullPower] = useState<boolean | null>(null)

  const refresh = useCallback(() => {
    playbooksApi.getSettings()
      .then((s) => setFullPower(s.plans_full_power))
      .catch(() => {})
  }, [])
  useEffect(() => { refresh() }, [refresh])

  const toggleFullPower = async () => {
    if (fullPower == null) return
    const next = !fullPower
    setFullPower(next) // optimistic; server response settles it
    try {
      const r = await playbooksApi.patchSettings({ plans_full_power: next })
      setFullPower(r.plans_full_power)
    } catch {
      setFullPower(!next)
    }
  }

  return (
    <div className="flex-1 overflow-y-auto">
      {/* Autonomy switch */}
      <div className="px-6 py-4 border-b border-white/5">
        <div className="text-[11px] uppercase tracking-[0.16em] text-ink-500 mb-1">Autonomy</div>
        <div className="flex items-center gap-4">
          <div className="flex-1 min-w-0">
            <p className="text-base font-semibold text-ink-50" data-testid="autonomy-headline">
              {fullPower == null
                ? '…'
                : fullPower
                  ? `Full power — ${agentName} publishes without asking`
                  : 'You approve each publish'}
            </p>
            <p className="text-xs text-ink-500 mt-0.5">
              A written plan is always required — this only controls whether each
              publish waits for your OK.
            </p>
          </div>
          <button
            onClick={toggleFullPower}
            disabled={fullPower == null}
            className={cn(
              'relative w-10 h-5 rounded-full transition-colors shrink-0',
              fullPower ? 'bg-emerald-600' : 'bg-ink-700',
            )}
            title={fullPower ? 'Turn approvals back on' : 'Let the agent publish on its own'}
            data-testid="autonomy-toggle"
          >
            <div className={cn(
              'absolute top-0.5 w-4 h-4 rounded-full bg-white transition-transform',
              fullPower ? 'left-[22px]' : 'left-0.5',
            )} />
          </button>
        </div>
      </div>

      {/* Plans list */}
      <PlansList agentName={agentName} />
    </div>
  )
}
