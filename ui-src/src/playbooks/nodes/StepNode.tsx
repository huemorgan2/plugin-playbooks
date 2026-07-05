/**
 * Custom reactflow node for every step kind.
 * Shape and color follow the visual grammar from PLAN.md.
 * Clicking opens a detail panel in the editor.
 */

import { memo, useEffect, useState } from 'react'
import { Handle, Position, type NodeProps } from '@xyflow/react'
import {
  Bot, Wrench, GitBranch, Layers, Clock, Mail,
  RotateCcw, CircleDot, Zap, ExternalLink, Info, Sparkles, Database, Ban,
} from 'lucide-react'
import { cn } from '../../lib/cn'
import { type StepKind, type RunStatus, STEP_COLORS, STATUS_COLORS } from '../types'

interface StepNodeData {
  stepId: string
  kind: StepKind
  label: string
  sublabel?: string
  explanation?: string
  runStatus?: RunStatus
  // 006.709: monotonically increasing marker — a new value means "this node
  // was just added/changed by the agent, play the glow-and-fade animation".
  glowSeq?: number
  // 007.009.01: a new value means "this node JUST executed in the run being
  // replayed" — play the flash-and-fade shimmer (separate from glowSeq so
  // build-glow and run-shimmer never fight).
  fireSeq?: number
  [key: string]: unknown
}

const KIND_ICONS: Record<StepKind, React.ComponentType<{ className?: string }>> = {
  agent_step: Bot,
  llm_step: Sparkles,
  tool_call: Wrench,
  condition: GitBranch,
  parallel: Layers,
  wait_for_approval: Clock,
  wait_for_event: Mail,
  subtask: ExternalLink,
  loop: RotateCcw,
  state: Database,
  halt: Ban,
}

function StepNodeComponent({ data, selected }: NodeProps) {
  const d = data as unknown as StepNodeData
  const kind = d.kind || 'tool_call'
  // Fallback guard: an unknown/transient kind must never white-screen the
  // canvas (it did, for `llm_step`, before it was added to STEP_COLORS).
  const colors = STEP_COLORS[kind] || STEP_COLORS.tool_call
  const Icon = KIND_ICONS[kind] || Zap
  const statusClass = d.runStatus ? STATUS_COLORS[d.runStatus] : ''

  const isCondition = kind === 'condition'

  // 006.709: massive glow that dies down to nothing. Re-triggers whenever
  // glowSeq changes (an edit re-glows an existing node).
  const [glowing, setGlowing] = useState(false)
  useEffect(() => {
    if (!d.glowSeq) return
    setGlowing(true)
    const t = setTimeout(() => setGlowing(false), 1600)
    return () => clearTimeout(t)
  }, [d.glowSeq])

  // 007.009.01: run-shimmer — flash in the node's own color when it executes
  // during a replay. Reuses --glow-rgb (the kind color) via .node-firing.
  const [firing, setFiring] = useState(false)
  useEffect(() => {
    if (!d.fireSeq) return
    setFiring(true)
    const t = setTimeout(() => setFiring(false), 1500)
    return () => clearTimeout(t)
  }, [d.fireSeq])

  return (
    <>
      <Handle type="target" position={Position.Top} className="!bg-ink-600 !w-2 !h-2 !border-ink-500" />
      <div
        style={{ ['--glow-rgb' as string]: colors.glow }}
        className={cn(
          'px-4 py-2.5 border backdrop-blur-sm shadow-lg transition-all min-w-[160px] max-w-[240px] cursor-pointer',
          colors.bg, colors.border,
          isCondition ? 'rotate-0 rounded-lg' : 'rounded-xl',
          selected && 'ring-2 ring-luna-400/50 ring-offset-1 ring-offset-ink-950',
          d.runStatus === 'running' && 'animate-pulse',
          glowing && 'node-arriving',
          firing && 'node-firing',
        )}
      >
        <div className="flex items-center gap-2">
          <div className={cn(
            'w-6 h-6 rounded-md flex items-center justify-center shrink-0',
            kind === 'agent_step' ? 'bg-indigo-800/60' :
            kind === 'tool_call' ? 'bg-teal-800/60' :
            kind === 'condition' ? 'bg-amber-800/60' :
            kind === 'wait_for_approval' || kind === 'wait_for_event' ? 'bg-orange-800/60' :
            kind === 'loop' ? 'bg-purple-800/60' :
            kind === 'state' ? 'bg-emerald-800/60' :
            kind === 'halt' ? 'bg-rose-800/60' :
            'bg-ink-800/60'
          )}>
            <Icon className={cn('w-3.5 h-3.5', colors.text)} />
          </div>
          <div className="min-w-0 flex-1">
            <div className={cn('text-xs font-medium truncate', colors.text)}>
              {d.label}
            </div>
            {d.sublabel && (
              <div className="text-[10px] text-ink-500 truncate">{d.sublabel}</div>
            )}
          </div>
          {d.runStatus && (
            <CircleDot className={cn('w-3 h-3 shrink-0', statusClass)} />
          )}
          {d.explanation && !d.runStatus && (
            <Info className="w-3 h-3 shrink-0 text-ink-600" />
          )}
        </div>
      </div>
      <Handle type="source" position={Position.Bottom} className="!bg-ink-600 !w-2 !h-2 !border-ink-500" />
    </>
  )
}

export const StepNode = memo(StepNodeComponent)
