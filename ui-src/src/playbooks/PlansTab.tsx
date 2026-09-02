/**
 * PlansTab (plans/016 phase 3) — the owner's audit trail of playbook
 * changes plus the autonomy switch. A plan is one row of readable text;
 * publishes stamp code-generated outcome facts onto it.
 */
import { useCallback, useEffect, useState } from 'react'
import { ChevronDown, ChevronRight, Loader2 } from 'lucide-react'
import { cn } from '../lib/cn'
import { playbooksApi, type PlanBrief, type PlanDetail } from './api'
import { timeAgo } from './editorBits'

// Status chips recolor border + text only, never the fill (ux_guidelines).
const PILL: Record<string, string> = {
  proposed: 'border-amber-500/40 text-amber-300',
  approved: 'border-emerald-500/40 text-emerald-300',
  rejected: 'border-rose-500/40 text-rose-300',
  done: 'border-white/15 text-ink-400',
}

export function PlanStatusPill({ status }: { status: string }) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full border px-2 py-px text-[10px] font-medium capitalize whitespace-nowrap',
        PILL[status] ?? 'border-white/15 text-ink-400',
      )}
      data-testid="plan-status-pill"
    >
      {status}
    </span>
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

function PlanRow({ plan, expanded, onToggle }: {
  plan: PlanBrief
  expanded: boolean
  onToggle: () => void
}) {
  const [detail, setDetail] = useState<PlanDetail | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!expanded || detail) return
    let cancelled = false
    playbooksApi.getPlan(plan.plan_id)
      .then((d) => { if (!cancelled) setDetail(d) })
      .catch((e) => { if (!cancelled) setError(e.message) })
    return () => { cancelled = true }
  }, [expanded, detail, plan.plan_id])

  const Chevron = expanded ? ChevronDown : ChevronRight
  return (
    <div className="border-b border-white/5" data-testid={`plan-row-${plan.plan_id}`}>
      <button
        onClick={onToggle}
        className="w-full flex items-center gap-3 px-6 py-3 text-left hover:bg-white/[.02] transition"
      >
        <Chevron className="w-3.5 h-3.5 text-ink-600 shrink-0" />
        <PlanStatusPill status={plan.status} />
        <span className="font-medium text-ink-100 truncate">{plan.title}</span>
        <span className="ml-auto shrink-0 text-[11px] text-ink-500 whitespace-nowrap">
          {plan.playbook_refs.length > 0 && (
            <span className="mr-3">{plan.playbook_refs.join(', ')}</span>
          )}
          {plan.created_at ? timeAgo(plan.created_at) : ''}
        </span>
      </button>
      {expanded && (
        <div className="px-6 pb-4 pl-[3.25rem] space-y-3" data-testid="plan-detail">
          {error ? (
            <p className="text-xs text-rose-400">{error}</p>
          ) : !detail ? (
            <Loader2 className="w-4 h-4 animate-spin text-ink-500" />
          ) : (
            <>
              <p className="text-sm text-ink-200 whitespace-pre-wrap">{renderInline(detail.body)}</p>
              {detail.rejection_note && (
                <p className="text-xs text-rose-300" data-testid="plan-rejection">
                  Rejected: {detail.rejection_note}
                </p>
              )}
              {(detail.outcome_facts?.length ?? 0) > 0 && (
                <div className="space-y-1" data-testid="plan-facts">
                  {detail.outcome_facts.map((f, i) => (
                    <p key={i} className="text-xs text-ink-400">
                      {factLine(f)}
                      {f.at && <span className="text-ink-600"> · {timeAgo(f.at)}</span>}
                    </p>
                  ))}
                </div>
              )}
              {detail.execution_summary && (
                <div data-testid="plan-summary">
                  <p className="text-[11px] uppercase tracking-[0.16em] text-ink-500 mb-1">
                    How it went
                  </p>
                  <p className="text-xs text-ink-300 whitespace-pre-wrap">
                    {renderInline(detail.execution_summary)}
                  </p>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

/**
 * The plans list on its own — reused by the section-level PlansTab (all
 * plans) and the editor's per-playbook Plans tab (plans/021: pass
 * `playbook` to narrow to plans referencing it).
 */
export function PlansList({ agentName, playbook }: { agentName: string; playbook?: string }) {
  const [plans, setPlans] = useState<PlanBrief[] | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)

  useEffect(() => {
    playbooksApi.listPlans(undefined, playbook)
      .then((r) => setPlans(r.plans))
      .catch(() => setPlans([]))
  }, [playbook])

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
          stamped onto it.
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
        <div>
          {plans.map((p) => (
            <PlanRow
              key={p.plan_id}
              plan={p}
              expanded={expanded === p.plan_id}
              onToggle={() => setExpanded((cur) => (cur === p.plan_id ? null : p.plan_id))}
            />
          ))}
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
