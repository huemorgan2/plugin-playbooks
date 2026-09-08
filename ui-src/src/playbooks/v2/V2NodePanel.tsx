/**
 * plans/032 phase 10 — the right-hand panel for a selected python graph
 * node: kind, tool, line range, the source slice it covers, and — when a
 * run is overlaid — every journal occurrence that landed on it (args,
 * result, error). A compute node shows the run-level error when the
 * failed line falls inside it.
 */
import { useState } from 'react'
import { X, ChevronDown, ChevronRight } from 'lucide-react'
import { cn } from '../../lib/cn'
import { STEP_COLORS, type PlaybookRunDetail, type TraceRow } from '../types'
import { Code } from '../explain/primitives'
import { JsonTree } from '../explain/jsontree'
import { IntegrationIcon, toolIconUrl, useIconRef } from '../icons'
import { kindIcon } from '../explain/primitives'
import { Code2, ShieldAlert } from 'lucide-react'
import { lookOf, type V2Item } from './types'

const KIND_LABELS: Record<string, string> = {
  tool: 'Tool', llm: 'LLM', agent: 'Agent', subtask: 'Subtask', approve: 'Approval',
  wait_event: 'Wait for event', now: 'Now', random: 'Random', log: 'Log', gather: 'Gather',
  if: 'Condition', for: 'Loop', while: 'Loop', compute: 'Compute', error_boundary: 'Error boundary',
}

function fmtMs(ms: number | null): string | null {
  if (ms == null) return null
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(2)}s`
}

function statusClass(status: string): string {
  return status === 'failed' ? 'bg-rose-900/40 text-rose-300'
    : status === 'running' ? 'bg-blue-900/40 text-blue-300'
    : status === 'waiting' ? 'bg-amber-900/40 text-amber-300'
    : 'bg-emerald-900/40 text-emerald-300'
}

export function V2NodePanel({
  item, code, run, onClose,
}: {
  item: V2Item
  code: string | null
  run: PlaybookRunDetail | null
  onClose: () => void
}) {
  const look = lookOf(item.kind)
  const colors = STEP_COLORS[look]
  const Icon = look === 'compute' ? Code2 : look === 'error_boundary' ? ShieldAlert : kindIcon(look as any)
  const iconRef = useIconRef()
  const tool = item.kind === 'tool' && 'sublabel' in item ? item.sublabel : null
  const toolUrl = tool ? toolIconUrl(iconRef, tool) : null
  const [showRaw, setShowRaw] = useState(false)

  const slice = code
    ? code.split('\n').slice(item.line - 1, item.end_line).join('\n')
    : ''
  const rows: TraceRow[] = run?.trace ? run.trace.filter((r) => r.node === item.node) : []
  const last = rows[rows.length - 1] ?? null
  // a compute (or container) node owns the run-level error when the failed
  // line falls inside it and no effect row failed
  const ownsRunError = !!run && run.status === 'failed' && run.failed_line != null
    && item.line <= run.failed_line && run.failed_line <= item.end_line
    && !run.trace?.some((r) => r.status === 'failed')

  return (
    <div className="w-[420px] shrink-0 border-l border-white/5 bg-ink-950/80 backdrop-blur-sm overflow-y-auto" data-testid="v2-node-panel">
      <div className="flex items-center justify-between px-4 py-3 border-b border-white/5">
        <div className="flex items-center gap-2 min-w-0">
          <div className={cn('w-6 h-6 rounded-md flex items-center justify-center shrink-0', 'bg-ink-800/60')}>
            <IntegrationIcon
              url={toolUrl}
              fallback={Icon}
              fallbackClass={cn('w-3.5 h-3.5', colors.text)}
              className="w-6 h-6 rounded-md"
            />
          </div>
          <span className={cn('text-[10px] uppercase tracking-[0.16em] font-semibold shrink-0', colors.text)} data-testid="v2-node-kind">
            {KIND_LABELS[item.kind] || item.kind}
          </span>
          <span className="text-[10px] font-mono text-ink-500 truncate" data-testid="v2-node-id">{item.node}</span>
        </div>
        <button
          onClick={onClose}
          className="p-1 rounded hover:bg-white/10 text-ink-500 hover:text-ink-200 transition"
          data-testid="v2-node-close"
        >
          <X className="w-3.5 h-3.5" />
        </button>
      </div>

      <div className="px-4 py-3 space-y-3">
        <div className="text-sm font-semibold text-ink-100 leading-snug" data-testid="v2-node-label">
          {item.label}
        </div>
        <div className="flex items-center gap-2 flex-wrap text-[10px] text-ink-500">
          {tool && <span className="font-mono text-teal-300" data-testid="v2-node-tool">{tool}</span>}
          <span data-testid="v2-node-lines">
            {item.line === item.end_line ? `line ${item.line}` : `lines ${item.line}–${item.end_line}`}
          </span>
          {'loop_depth' in item && item.loop_depth > 0 && <span className="text-purple-300">in loop</span>}
          {'in_try' in item && item.in_try && <span className="text-rose-300">in try</span>}
        </div>

        {slice && (
          <div data-testid="v2-node-source">
            <Code source={slice} />
          </div>
        )}

        {run && (
          <div className="space-y-2 pt-2 border-t border-white/5" data-testid="v2-node-exec">
            <div className="flex items-center gap-2">
              <div className="text-[10px] uppercase tracking-wider text-ink-600">Execution</div>
              {rows.length > 1 && (
                <span className="text-[9px] text-ink-500 px-1 py-0.5 rounded bg-ink-800" data-testid="v2-node-runs">
                  {rows.length} runs
                </span>
              )}
            </div>

            {ownsRunError && (
              <div data-testid="v2-node-run-error">
                <div className="text-[10px] text-ink-500 mb-0.5">
                  Failed here{run.error_type ? ` · ${run.error_type}` : ''}
                </div>
                <pre className="text-[10px] text-rose-300 font-mono whitespace-pre-wrap bg-rose-950/30 rounded p-2 max-h-40 overflow-auto">
                  {run.error}
                </pre>
                {run.traceback && (
                  <pre className="mt-1 text-[10px] text-ink-400 font-mono whitespace-pre-wrap bg-ink-900/60 rounded p-2 max-h-40 overflow-auto">
                    {run.traceback}
                  </pre>
                )}
              </div>
            )}

            {!last ? (
              !ownsRunError && <p className="text-xs text-ink-600 italic">Did not run in the selected run.</p>
            ) : (
              <>
                <table className="w-full text-[10px]" data-testid="v2-node-occurrences">
                  <tbody>
                    {rows.map((r) => (
                      <tr key={r.seq} className="border-t border-white/5" data-testid="v2-node-occurrence" data-status={r.status}>
                        <td className="py-1 pr-2 text-ink-500 font-mono">#{r.occurrence}</td>
                        <td className="py-1 pr-2">
                          <span className={cn('px-1.5 py-0.5 rounded font-medium', statusClass(r.status))}>
                            {r.status}
                          </span>
                        </td>
                        <td className="py-1 pr-2 text-ink-500">{r.journal_status !== r.status ? r.journal_status : ''}</td>
                        <td className="py-1 pr-2 text-ink-500">{fmtMs(r.ms)}</td>
                        <td className="py-1 text-ink-500">{r.dry ? 'dry' : ''}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>

                {last.parked_on && (
                  <div className="text-[10px] text-amber-300" data-testid="v2-node-parked">
                    waiting on {String(last.parked_on.kind ?? 'something')}
                    {last.parked_on.event_name ? ` · ${String(last.parked_on.event_name)}` : ''}
                  </div>
                )}

                {last.error && (
                  <div>
                    <div className="text-[10px] text-ink-500 mb-0.5">Error · {last.error.type}</div>
                    <pre className="text-[10px] text-rose-300 font-mono whitespace-pre-wrap bg-rose-950/30 rounded p-2 max-h-40 overflow-auto" data-testid="v2-node-error">
                      {last.error.message}
                    </pre>
                  </div>
                )}

                {(last.args != null || last.result != null) && (
                  <div>
                    <button
                      onClick={() => setShowRaw(!showRaw)}
                      className="flex items-center gap-1 text-[10px] text-ink-500 hover:text-ink-300 transition"
                      data-testid="v2-node-raw-toggle"
                    >
                      {showRaw ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                      Raw data
                    </button>
                    {showRaw && (
                      <div className="mt-1 space-y-2">
                        {last.args != null && (
                          <div>
                            <div className="text-[10px] text-ink-500 mb-0.5">Args</div>
                            <JsonTree data={last.args} />
                          </div>
                        )}
                        {last.result != null && (
                          <div>
                            <div className="text-[10px] text-ink-500 mb-0.5">Result</div>
                            <JsonTree data={last.result} />
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
      </div>
    </div>
  )
}
