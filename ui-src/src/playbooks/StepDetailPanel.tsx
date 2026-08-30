/**
 * StepDetailPanel (plans/016 phase 4) — the right-hand explainer for a
 * selected step, with its execution detail when a run is overlaid. Lifted
 * out of PlaybookEditor so the Versions tab can show it per version.
 */
import { useState } from 'react'
import { X, ChevronDown, ChevronRight } from 'lucide-react'
import { cn } from '../lib/cn'
import type { StepDef, StepKind, StepRunDetail, PlaybookRunDetail } from './types'
import { STEP_COLORS } from './types'
import { KIND_LABELS, kindIcon } from './explain/primitives'
import { IntegrationIcon, toolIconUrl, useIconRef } from './icons'
import { headline } from './explain/headline'
import { STEP_EXPLAINERS, DataFlow, FooterChips } from './explain/registry'
import { JsonTree } from './explain/jsontree'

// All run-detail rows for one step id (a loop body step runs once per iteration).
export function execRowsForStep(run: PlaybookRunDetail | null, stepId: string): StepRunDetail[] {
  if (!run) return []
  return run.steps.filter((s) => s.step_id === stepId)
}

// 006.714: one plain-English sentence describing what a step DID in a run, so a
// human reads the outcome instead of decoding JSON. Raw data stays one click away.
function execSummary(step: StepDef, exec: StepRunDetail): string {
  const kind = step.kind
  if (exec.status === 'failed') {
    const first = (exec.error || '').split('\n')[0]?.trim()
    return first ? `Failed: ${first}` : 'This step failed.'
  }
  if (exec.status === 'running') return 'Running now…'
  if (exec.status === 'waiting') {
    return kind === 'wait_for_approval' ? 'Waiting for approval.' : 'Waiting for an event.'
  }
  if (exec.status === 'cancelled') return 'This step was cancelled.'
  // succeeded
  switch (kind) {
    case 'tool_call':
      return step.tool ? `Called \`${step.tool}\` — succeeded.` : 'Ran a tool — succeeded.'
    case 'agent_step':
      return 'The agent ran and produced a result.'
    case 'llm_step':
      return 'Generated a result.'
    case 'condition': {
      const branch = exec.outputs?.branch ?? exec.outputs?.taken
      return branch ? `Condition took the \`${branch}\` branch.` : 'Condition was evaluated.'
    }
    case 'loop': {
      const n = exec.outputs?.iterations
      const stopped = exec.outputs?.stopped
      const base = n != null ? `Ran ${n} iteration${Number(n) === 1 ? '' : 's'}.` : 'Looped over the items.'
      return stopped ? `${base} (stopped: ${stopped})` : base
    }
    case 'parallel':
      return 'Ran its parallel branches.'
    case 'subtask':
      return step.playbook ? `Ran the \`${step.playbook}\` sub-playbook.` : 'Ran a sub-playbook.'
    case 'state':
      return 'Updated the run state.'
    case 'wait_for_approval':
      return 'Approval was granted.'
    case 'wait_for_event':
      return 'The awaited event arrived.'
    case 'halt':
      return 'Ended the run early (success).'
    default:
      return 'Completed.'
  }
}

function fmtDuration(start: string | null, end: string | null): string | null {
  if (!start || !end) return null
  const ms = new Date(end).getTime() - new Date(start).getTime()
  if (ms < 0) return null
  if (ms < 1000) return `${ms}ms`
  return `${(ms / 1000).toFixed(2)}s`
}

export function StepDetailPanel({
  step, execRows, hasRun, onClose, onSelectStep, onJumpToStep,
}: {
  step: StepDef
  execRows: StepRunDetail[]
  hasRun: boolean
  onClose: () => void
  onSelectStep: (s: StepDef) => void
  onJumpToStep: (id: string) => void
}) {
  const kind = step.kind as StepKind
  const colors = STEP_COLORS[kind] || STEP_COLORS.tool_call
  const Icon = kindIcon(kind)
  // plans/011: a tool step's header wears the integration icon.
  const iconRef = useIconRef()
  const toolUrl = kind === 'tool_call' ? toolIconUrl(iconRef, step.tool) : null
  // plans/010: per-kind explain renderer, 100% derived from the definition.
  const Explainer = STEP_EXPLAINERS[kind] || STEP_EXPLAINERS.tool_call
  // Loop bodies run once per iteration → many rows. Show the last execution and
  // note the count.
  const lastExec = execRows[execRows.length - 1] || null
  // 006.714: raw JSON is hidden by default — humans read the one-line summary.
  const [showRaw, setShowRaw] = useState(false)
  const [showRawDef, setShowRawDef] = useState(false)

  return (
    <div className="w-[420px] shrink-0 border-l border-white/5 bg-ink-950/80 backdrop-blur-sm overflow-y-auto">
      {/* Eyebrow row: kind + id. The bottom line lives right below as the headline. */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-white/5">
        <div className="flex items-center gap-2 min-w-0">
          <div className={cn(
            'w-6 h-6 rounded-md flex items-center justify-center shrink-0',
            kind === 'agent_step' ? 'bg-indigo-800/60' :
            kind === 'llm_step' ? 'bg-fuchsia-800/60' :
            kind === 'tool_call' ? 'bg-teal-800/60' :
            kind === 'condition' ? 'bg-amber-800/60' :
            kind === 'wait_for_approval' || kind === 'wait_for_event' ? 'bg-orange-800/60' :
            kind === 'subtask' ? 'bg-violet-800/60' :
            kind === 'loop' ? 'bg-purple-800/60' :
            kind === 'state' ? 'bg-emerald-800/60' :
            kind === 'code' ? 'bg-cyan-800/60' :
            kind === 'halt' ? 'bg-rose-800/60' :
            'bg-ink-800/60'
          )}>
            <IntegrationIcon
              url={toolUrl}
              fallback={Icon}
              fallbackClass={cn('w-3.5 h-3.5', colors.text)}
              className="w-6 h-6 rounded-md"
            />
          </div>
          <span className={cn('text-[10px] uppercase tracking-[0.16em] font-semibold shrink-0', colors.text)}>
            {KIND_LABELS[kind] || kind}
          </span>
          <span className="text-[10px] font-mono text-ink-500 truncate">{step.id}</span>
        </div>
        <button
          onClick={onClose}
          className="p-1 rounded hover:bg-white/10 text-ink-500 hover:text-ink-200 transition"
        >
          <X className="w-3.5 h-3.5" />
        </button>
      </div>

      <div className="px-4 py-3 space-y-3">
        {/* Bottom line first — generated from the definition, never from prose. */}
        <div className="text-sm font-semibold text-ink-100 leading-snug" data-testid="step-headline">
          {headline(step)}
        </div>

        {step.explanation && (
          <p className="text-xs text-ink-400 leading-relaxed border-l-2 border-white/10 pl-2.5">
            {step.explanation}
          </p>
        )}

        <Explainer step={step} onSelectStep={onSelectStep} />

        <FooterChips step={step} />

        <DataFlow step={step} onJumpToStep={onJumpToStep} />

        {/* 007.009.01: execution detail for the selected run */}
        {hasRun && (
          <div className="space-y-2 pt-2 border-t border-white/5">
            <div className="flex items-center gap-2">
              <div className="text-[10px] uppercase tracking-wider text-ink-600">Execution</div>
              {execRows.length > 1 && (
                <span className="text-[9px] text-ink-500 px-1 py-0.5 rounded bg-ink-800">
                  {execRows.length} runs
                </span>
              )}
            </div>

            {!lastExec ? (
              <p className="text-xs text-ink-600 italic">Did not run in the selected run.</p>
            ) : (
              <>
                <div className="flex items-center gap-2 flex-wrap text-[10px]">
                  <span className={cn(
                    'px-1.5 py-0.5 rounded font-medium',
                    lastExec.status === 'failed' ? 'bg-rose-900/40 text-rose-300' :
                    lastExec.status === 'running' ? 'bg-blue-900/40 text-blue-300' :
                    lastExec.status === 'waiting' ? 'bg-amber-900/40 text-amber-300' :
                    'bg-emerald-900/40 text-emerald-300',
                  )}>
                    {lastExec.status}
                  </span>
                  {fmtDuration(lastExec.started_at, lastExec.completed_at) && (
                    <span className="text-ink-500">{fmtDuration(lastExec.started_at, lastExec.completed_at)}</span>
                  )}
                  {lastExec.cost_cents != null && lastExec.cost_cents > 0 && (
                    <span className="text-ink-500">${(lastExec.cost_cents / 100).toFixed(4)}</span>
                  )}
                  {lastExec.retry_count != null && lastExec.retry_count > 0 && (
                    <span className="text-amber-400">{lastExec.retry_count} retr{lastExec.retry_count === 1 ? 'y' : 'ies'}</span>
                  )}
                  {kind === 'loop' && lastExec.outputs?.iterations != null && (
                    <span className="text-purple-300">{String(lastExec.outputs.iterations)} iterations</span>
                  )}
                  {kind === 'loop' && lastExec.outputs?.stopped && (
                    <span className="text-amber-400">stopped: {String(lastExec.outputs.stopped)}</span>
                  )}
                </div>

                {/* 006.714: plain-English summary first — humans before JSON. */}
                <p className="text-xs text-ink-300 leading-relaxed">
                  {execSummary(step, lastExec)}
                </p>

                {lastExec.error && (
                  <div>
                    <div className="text-[10px] text-ink-500 mb-0.5">Error</div>
                    <pre className="text-[10px] text-rose-300 font-mono whitespace-pre-wrap bg-rose-950/30 rounded p-2 max-h-40 overflow-auto">
                      {lastExec.error}
                    </pre>
                  </div>
                )}

                {((lastExec.inputs && Object.keys(lastExec.inputs).length > 0) || lastExec.outputs != null) && (
                  <div>
                    <button
                      onClick={() => setShowRaw((v) => !v)}
                      className="flex items-center gap-1 text-[10px] text-ink-500 hover:text-ink-300 transition"
                    >
                      {showRaw ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                      {showRaw ? 'Hide raw data' : 'Show raw input / output'}
                    </button>
                    {showRaw && (
                      <div className="mt-2 space-y-2">
                        {lastExec.inputs && Object.keys(lastExec.inputs).length > 0 && (
                          <div>
                            <div className="text-[10px] text-ink-500 mb-0.5">Resolved inputs</div>
                            <JsonTree data={lastExec.inputs} />
                          </div>
                        )}
                        {lastExec.outputs != null && (
                          <div>
                            <div className="text-[10px] text-ink-500 mb-0.5">Output</div>
                            <JsonTree data={lastExec.outputs} />
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                )}
              </>
            )}
          </div>
        )}

        {/* plans/010: the escape hatch — the exact stored definition, collapsed. */}
        <div className="pt-2 border-t border-white/5">
          <button
            onClick={() => setShowRawDef((v) => !v)}
            className="flex items-center gap-1 text-[10px] text-ink-500 hover:text-ink-300 transition"
            data-testid="raw-def-toggle"
          >
            {showRawDef ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
            Raw definition
          </button>
          {showRawDef && (
            <pre className="mt-2 text-[10px] text-ink-300 font-mono whitespace-pre-wrap bg-ink-900/60 rounded p-2 max-h-64 overflow-auto">
              {JSON.stringify(step, null, 2)}
            </pre>
          )}
        </div>
      </div>
    </div>
  )
}

