/**
 * plans/010: per-kind explain renderers, dispatched by step.kind.
 *
 * Each component receives ONLY the step definition (plus a canvas-select
 * callback for nested steps) and renders a colored, structured visualization
 * of it. Zero-data rule: no sample values, no fallback text standing in for
 * content — an absent field renders nothing.
 */

import { cn } from '../../lib/cn'
import type { StepDef, StepKind } from '../types'
import {
  Code, Expr, KVTable, NamePill, SchemaTree, SectionLabel, StepList,
  TemplateText, Value,
} from './primitives'
import { stepReads, stepWrites } from './dataflow'

export interface ExplainProps {
  step: StepDef
  onSelectStep?: (step: StepDef) => void
}

function ToolCallExplain({ step }: ExplainProps) {
  return (
    <div className="space-y-3">
      {step.tool && (
        <div>
          <SectionLabel>Tool</SectionLabel>
          <NamePill text={step.tool} className="border-teal-500/30 text-teal-200 bg-teal-950/40" />
        </div>
      )}
      {step.args && Object.keys(step.args).length > 0 && (
        <div>
          <SectionLabel>Arguments</SectionLabel>
          <KVTable data={step.args} />
        </div>
      )}
    </div>
  )
}

function CodeExplain({ step }: ExplainProps) {
  return (
    <div className="space-y-3">
      {step.code_inputs && Object.keys(step.code_inputs).length > 0 && (
        <div>
          <SectionLabel>Inputs</SectionLabel>
          <KVTable data={step.code_inputs} />
        </div>
      )}
      {step.source && (
        <div>
          <SectionLabel>Source</SectionLabel>
          <Code source={step.source} />
        </div>
      )}
      <p className="text-[10px] text-ink-500">
        The return value becomes <code className="font-mono text-violet-300">steps.{step.id}.result</code>
      </p>
    </div>
  )
}

function AgentStepExplain({ step }: ExplainProps) {
  return (
    <div className="space-y-3">
      {step.prompt && (
        <div>
          <SectionLabel>Prompt</SectionLabel>
          <TemplateText text={step.prompt} />
        </div>
      )}
      {step.tools && step.tools.length > 0 && (
        <div>
          <SectionLabel>Tools the agent may use</SectionLabel>
          <div className="flex flex-wrap gap-1">
            {step.tools.map((t) => (
              <NamePill key={t} text={t} className="border-indigo-500/30 text-indigo-200 bg-indigo-950/40" />
            ))}
          </div>
        </div>
      )}
      {step.output_schema && (
        <div>
          <SectionLabel>Produces</SectionLabel>
          <SchemaTree schema={step.output_schema} />
        </div>
      )}
    </div>
  )
}

function LlmStepExplain({ step }: ExplainProps) {
  return (
    <div className="space-y-3">
      {(step.purpose || step.model) && (
        <div className="flex flex-wrap gap-1.5">
          {step.purpose && (
            <NamePill text={step.purpose} className="border-fuchsia-500/30 text-fuchsia-200 bg-fuchsia-950/40" />
          )}
          {step.model && (
            <NamePill text={step.model} className="border-white/10 text-ink-300 bg-ink-900/60" />
          )}
        </div>
      )}
      {step.system && (
        <div>
          <SectionLabel>System</SectionLabel>
          <TemplateText text={step.system} />
        </div>
      )}
      {step.prompt && (
        <div>
          <SectionLabel>Prompt</SectionLabel>
          <TemplateText text={step.prompt} />
        </div>
      )}
      {step.output_schema && (
        <div>
          <SectionLabel>Produces</SectionLabel>
          <SchemaTree schema={step.output_schema} />
        </div>
      )}
    </div>
  )
}

function ConditionExplain({ step, onSelectStep }: ExplainProps) {
  return (
    <div className="space-y-2">
      {step.when && (
        <div className="rounded-lg border border-amber-500/20 bg-amber-950/20 px-2.5 py-2">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-amber-400 mr-2">If</span>
          <Expr expr={step.when} />
        </div>
      )}
      {step.then && step.then.length > 0 && (
        <div className="rounded-lg border border-emerald-500/15 bg-emerald-950/10 px-2 py-1.5" data-testid="branch-then">
          <div className="text-[10px] font-semibold uppercase tracking-wider text-emerald-400 px-1 mb-1">Then</div>
          <StepList steps={step.then} onSelect={onSelectStep} />
        </div>
      )}
      {step.else && step.else.length > 0 && (
        <div className="rounded-lg border border-rose-500/15 bg-rose-950/10 px-2 py-1.5" data-testid="branch-else">
          <div className="text-[10px] font-semibold uppercase tracking-wider text-rose-400 px-1 mb-1">Otherwise</div>
          <StepList steps={step.else} onSelect={onSelectStep} />
        </div>
      )}
    </div>
  )
}

function LoopExplain({ step, onSelectStep }: ExplainProps) {
  const guards: { label: string; expr: string }[] = []
  if (step.while) guards.push({ label: 'While', expr: step.while })
  if (step.until) guards.push({ label: 'Until', expr: step.until })
  if (step.break_when) guards.push({ label: 'Break when', expr: step.break_when })
  return (
    <div className="space-y-3">
      {step.over != null && (
        <div className="rounded-lg border border-purple-500/20 bg-purple-950/20 px-2.5 py-2 text-[11px]">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-purple-400 mr-2">For each</span>
          {step.item_name && (
            <>
              <span className="font-mono text-emerald-300">{step.item_name}</span>
              <span className="text-ink-500"> in </span>
            </>
          )}
          {typeof step.over === 'string'
            ? <Expr expr={step.over} />
            : <span className="font-mono"><Value value={step.over} /></span>}
        </div>
      )}
      {guards.map((g) => (
        <div key={g.label} className="flex items-baseline gap-2 text-[11px]">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-purple-400 shrink-0">{g.label}</span>
          <Expr expr={g.expr} />
        </div>
      ))}
      <div className="flex flex-wrap gap-1.5">
        {step.concurrency != null && step.concurrency > 1 && (
          <NamePill text={`${step.concurrency} at a time`} className="border-purple-500/30 text-purple-200 bg-purple-950/40" />
        )}
        {step.max_iterations != null && step.max_iterations !== 100 && (
          <NamePill text={`≤ ${step.max_iterations} iterations`} className="border-white/10 text-ink-300 bg-ink-900/60" />
        )}
      </div>
      {step.collect && (
        <div className="text-[11px]">
          <SectionLabel>Collect each iteration</SectionLabel>
          <Expr expr={step.collect} />
          <span className="text-ink-500"> → </span>
          <code className="font-mono text-[11px] text-violet-300">steps.{step.id}.collected</code>
        </div>
      )}
      {step.body && step.body.length > 0 && (
        <div>
          <SectionLabel>Body</SectionLabel>
          <StepList steps={step.body} onSelect={onSelectStep} />
        </div>
      )}
    </div>
  )
}

function ParallelExplain({ step, onSelectStep }: ExplainProps) {
  return (
    <div className="space-y-2">
      {(step.branches || []).map((branch, i) => (
        <div key={i} className="rounded-lg border border-sky-500/15 bg-sky-950/10 px-2 py-1.5">
          <div className="text-[10px] font-semibold uppercase tracking-wider text-sky-400 px-1 mb-1">
            Branch {i + 1}
          </div>
          <StepList steps={branch} onSelect={onSelectStep} />
        </div>
      ))}
      {step.fan_in && (
        <p className="text-[11px] text-ink-400">
          Continues when <span className="font-mono text-sky-300">{step.fan_in}</span> branches finish
        </p>
      )}
    </div>
  )
}

function WaitForEventExplain({ step }: ExplainProps) {
  return (
    <div className="space-y-3">
      {step.event && (
        <div>
          <SectionLabel>Event</SectionLabel>
          <NamePill text={step.event} className="border-orange-500/30 text-orange-200 bg-orange-950/40" />
        </div>
      )}
      {step.event_filter && Object.keys(step.event_filter).length > 0 && (
        <div>
          <SectionLabel>Only when</SectionLabel>
          <KVTable data={step.event_filter} />
        </div>
      )}
    </div>
  )
}

function WaitForApprovalExplain({ step }: ExplainProps) {
  if (!step.show?.length) return <div />
  return (
    <div>
      <SectionLabel>Shows you</SectionLabel>
      <div className="flex flex-wrap gap-1">
        {step.show.map((s) => (
          <code key={s} className="font-mono text-[11px] px-1.5 py-0.5 rounded bg-ink-900/80 border border-white/5">
            <Expr expr={s} />
          </code>
        ))}
      </div>
    </div>
  )
}

function SubtaskExplain({ step }: ExplainProps) {
  return (
    <div className="space-y-3">
      {step.playbook && (
        <div>
          <SectionLabel>Playbook</SectionLabel>
          <NamePill text={step.playbook} className="border-violet-500/30 text-violet-200 bg-violet-950/40" />
        </div>
      )}
      {step.inputs_map && Object.keys(step.inputs_map).length > 0 && (
        <div>
          <SectionLabel>Its inputs</SectionLabel>
          <KVTable data={step.inputs_map} arrow />
        </div>
      )}
      {step.returns && Object.keys(step.returns).length > 0 && (
        <div>
          <SectionLabel>Returns to this run</SectionLabel>
          <KVTable data={step.returns} arrow />
        </div>
      )}
    </div>
  )
}

function StateExplain({ step }: ExplainProps) {
  const ops = step.state || []
  if (!ops.length) return <div />
  return (
    <div className="space-y-1.5">
      {ops.map((op, i) => (
        <div key={i} className="flex items-baseline gap-2 text-[11px] flex-wrap" data-state-op={op.op}>
          <span className="px-1.5 py-px rounded text-[10px] font-mono font-medium bg-emerald-900/40 text-emerald-300 shrink-0">
            {op.op}
          </span>
          <span className="font-mono text-emerald-200">{op.var}</span>
          {op.value !== undefined && op.value !== null && (
            <>
              <span className="text-ink-600">=</span>
              <span className="font-mono min-w-0 break-words"><Value value={op.value} /></span>
            </>
          )}
          {op.into && (
            <>
              <span className="text-ink-600">→</span>
              <span className="font-mono text-emerald-200">{op.into}</span>
            </>
          )}
        </div>
      ))}
    </div>
  )
}

function HaltExplain({ step }: ExplainProps) {
  return (
    <div className="space-y-2">
      {step.when && (
        <div className="rounded-lg border border-rose-500/20 bg-rose-950/20 px-2.5 py-2">
          <span className="text-[10px] font-semibold uppercase tracking-wider text-rose-400 mr-2">If</span>
          <Expr expr={step.when} />
        </div>
      )}
      {step.value !== undefined && step.value !== null && (
        <div>
          <SectionLabel>The run returns</SectionLabel>
          <div className="font-mono text-[11px]"><Value value={step.value} /></div>
        </div>
      )}
    </div>
  )
}

export const STEP_EXPLAINERS: Record<StepKind, React.ComponentType<ExplainProps>> = {
  tool_call: ToolCallExplain,
  code: CodeExplain,
  agent_step: AgentStepExplain,
  llm_step: LlmStepExplain,
  condition: ConditionExplain,
  loop: LoopExplain,
  parallel: ParallelExplain,
  wait_for_event: WaitForEventExplain,
  wait_for_approval: WaitForApprovalExplain,
  subtask: SubtaskExplain,
  state: StateExplain,
  halt: HaltExplain,
}

const REF_CLS = (ref: string) =>
  ref.startsWith('inputs.') ? 'border-sky-500/30 text-sky-300'
    : ref.startsWith('steps.') ? 'border-violet-500/30 text-violet-300'
    : 'border-emerald-500/30 text-emerald-300'

/**
 * Derived reads/writes chips. Clicking a steps.* read jumps to that step's
 * node on the canvas.
 */
export function DataFlow({
  step, onJumpToStep,
}: {
  step: StepDef
  onJumpToStep?: (stepId: string) => void
}) {
  const reads = stepReads(step)
  const writes = stepWrites(step)
  if (!reads.length && !writes.length) return null
  const chip = (ref: string, clickable: boolean) => {
    const cls = cn(
      'px-1.5 py-0.5 rounded-full border text-[10px] font-mono bg-ink-950/60',
      REF_CLS(ref),
      clickable && 'hover:bg-white/5 cursor-pointer transition',
    )
    const target = ref.startsWith('steps.') ? ref.split('.')[1] : null
    return clickable && target ? (
      <button key={ref} className={cls} onClick={() => onJumpToStep?.(target)} data-ref={ref}>
        {ref}
      </button>
    ) : (
      <span key={ref} className={cls} data-ref={ref}>{ref}</span>
    )
  }
  return (
    <div className="space-y-2 pt-2 border-t border-white/5" data-testid="data-flow">
      {reads.length > 0 && (
        <div className="flex items-baseline gap-2 flex-wrap">
          <span className="text-[10px] uppercase tracking-wider text-ink-600 shrink-0">Reads</span>
          {reads.map((r) => chip(r, true))}
        </div>
      )}
      {writes.length > 0 && (
        <div className="flex items-baseline gap-2 flex-wrap">
          <span className="text-[10px] uppercase tracking-wider text-ink-600 shrink-0">Writes</span>
          {writes.map((w) => chip(w, false))}
        </div>
      )}
    </div>
  )
}

/** Error-handling chips shared by every kind; only non-defaults render. */
export function FooterChips({ step }: { step: StepDef }) {
  const chips: string[] = []
  if (step.retry?.max) chips.push(`retries ${step.retry.max}× (${step.retry.backoff_seconds}s backoff)`)
  if (step.on_error && step.on_error !== 'abort') chips.push(`on error: ${step.on_error}`)
  if (step.timeout_seconds) chips.push(`timeout ${step.timeout_seconds}s`)
  if (step.timeout) chips.push(`timeout ${step.timeout}s`)
  if (!chips.length) return null
  return (
    <div className="flex flex-wrap gap-1.5" data-testid="footer-chips">
      {chips.map((c) => (
        <span key={c} className="px-1.5 py-0.5 rounded-full border border-white/10 text-[10px] text-ink-400 bg-ink-900/60">
          {c}
        </span>
      ))}
    </div>
  )
}
