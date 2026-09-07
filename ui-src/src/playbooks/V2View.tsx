// plans/032 phase 04: the interim view for a python playbook — the source
// beside the selected run's step list. The graph view (plugin/10) replaces
// this file; nothing here calls `buildGraph` (a python definition is the
// checker summary, not a step list — there is no graph to lay out).
import { X } from 'lucide-react'
import { cn } from '../lib/cn'
import { Code } from './explain/primitives'
import type { PlaybookRunDetail, StepRunDetail } from './types'

export function V2View({
  code,
  runDetail,
  agentName,
  onClearRun,
}: {
  code: string | null
  runDetail: PlaybookRunDetail | null
  agentName: string
  onClearRun?: () => void
}) {
  return (
    <div className="h-full p-4 overflow-auto" data-testid="v2-view">
      <div className="text-[11px] uppercase tracking-[0.16em] text-ink-500 mb-2" data-testid="v2-header">
        python playbook — the graph view arrives with the canvas phase
      </div>
      <div className={cn('flex gap-4 min-w-0', runDetail ? 'flex-col lg:flex-row' : 'flex-col')}>
        <div className="flex-1 min-w-0" data-testid="v2-code">
          {code != null && code !== '' ? (
            <Code source={code} />
          ) : (
            <p className="text-[11px] text-ink-600">No source stored for this version.</p>
          )}
        </div>
        {runDetail && (
          <div className="lg:w-[340px] shrink-0 min-w-0" data-testid="v2-run">
            <div className="flex items-center gap-2 mb-1">
              <span className="text-[11px] uppercase tracking-[0.16em] text-ink-500">
                Run · {runDetail.id.slice(0, 8)}
              </span>
              <span className={cn('text-[10px] capitalize',
                runDetail.status === 'failed' ? 'text-rose-400' :
                runDetail.status === 'running' ? 'text-blue-400' :
                runDetail.status === 'waiting' ? 'text-amber-400' :
                'text-emerald-400',
              )}>
                {runDetail.status}
              </span>
              <span className="flex-1" />
              {onClearRun && (
                <button
                  onClick={onClearRun}
                  className="p-0.5 rounded hover:bg-white/10 text-ink-500 hover:text-ink-200 transition"
                  title="Clear run"
                  data-testid="v2-clear-run"
                >
                  <X className="w-3 h-3" />
                </button>
              )}
            </div>
            {runDetail.steps.length === 0 ? (
              <p className="text-[11px] text-ink-600">No effects recorded.</p>
            ) : (
              <div className="rounded-lg border border-white/5 bg-ink-900/40 divide-y divide-white/5">
                {runDetail.steps.map((s, i) => (
                  <V2StepRow key={`${s.step_id}-${i}`} step={s} />
                ))}
              </div>
            )}
          </div>
        )}
      </div>
      <p className="text-[11px] text-ink-600 mt-2">
        {agentName} writes this — ask in chat to change the playbook.
      </p>
    </div>
  )
}

// One effect of the run: id, kind, status, and the error when it failed —
// the same dot colours RunsTab's StepExecRow uses.
function V2StepRow({ step }: { step: StepRunDetail }) {
  return (
    <div className="px-2 py-1.5" data-testid="v2-step" data-status={step.status}>
      <div className="flex items-center gap-2">
        <span className={cn('w-1.5 h-1.5 rounded-full shrink-0',
          step.status === 'failed' ? 'bg-rose-400' :
          step.status === 'running' ? 'bg-blue-400 animate-pulse' :
          step.status === 'waiting' ? 'bg-amber-400' :
          step.status === 'cancelled' ? 'bg-ink-500' :
          'bg-emerald-400',
        )} />
        <span className="text-xs font-mono text-ink-200 truncate" data-testid="v2-step-id">{step.step_id}</span>
        <span className="text-[10px] text-ink-600 capitalize shrink-0" data-testid="v2-step-kind">
          {step.kind.replace(/_/g, ' ')}
        </span>
        <span className="flex-1" />
        <span className="text-[10px] text-ink-500 capitalize shrink-0" data-testid="v2-step-status">{step.status}</span>
      </div>
      {step.error && (
        <div className="mt-1 text-[11px] text-rose-300 font-mono whitespace-pre-wrap break-words" data-testid="v2-step-error">
          {step.error}
        </div>
      )}
    </div>
  )
}
