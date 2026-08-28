/**
 * plans/010: per-kind explain renderers, dispatched by step.kind.
 *
 * Each component receives ONLY the step definition (plus a canvas-select
 * callback for nested steps) and renders a colored, structured visualization
 * of it. Kinds without a renderer yet fall back to the legacy row list in
 * the panel (removed in phase 2).
 */

import type { StepDef, StepKind } from '../types'
import { Code, KVTable, NamePill, SectionLabel } from './primitives'

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

export const STEP_EXPLAINERS: Partial<Record<StepKind, React.ComponentType<ExplainProps>>> = {
  tool_call: ToolCallExplain,
  code: CodeExplain,
}
