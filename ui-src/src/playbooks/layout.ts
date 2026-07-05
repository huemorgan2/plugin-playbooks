/**
 * Convert a PlaybookDef into reactflow nodes + edges.
 * Layout is top-to-bottom with auto positioning.
 */

import type { Node, Edge } from '@xyflow/react'
import type { PlaybookDef, StepDef, StepRunDetail, RunStatus } from './types'

const NODE_H = 56
const GAP_Y = 60
const GAP_X = 260

interface LayoutContext {
  nodes: Node[]
  edges: Edge[]
  y: number
  runSteps?: Map<string, StepRunDetail>
  fireSeq?: Map<string, number>
}

function stepLabel(step: StepDef): string {
  switch (step.kind) {
    case 'tool_call':
      return step.tool || step.id
    case 'agent_step':
      return step.prompt?.slice(0, 40) || step.id
    case 'condition':
      return step.when?.slice(0, 30) || step.id
    case 'wait_for_approval':
      return 'Approve?'
    case 'wait_for_event':
      return step.event || 'Wait for event'
    case 'subtask':
      return `→ ${step.playbook || step.id}`
    case 'loop': {
      const ctrl = step.over ? `over ${step.over}`
        : step.while ? `while ${step.while}`
        : step.until ? `until ${step.until}`
        : step.id
      return `Loop: ${ctrl}`.slice(0, 44)
    }
    case 'parallel':
      return `Parallel (${step.branches?.length || 0})`
    case 'state': {
      const ops = step.state || []
      if (ops.length === 1) return `${ops[0]!.op} ${ops[0]!.var}`
      if (ops.length > 1) return `state (${ops.length} ops)`
      return 'state'
    }
    case 'halt':
      return step.when ? `Halt if ${step.when}`.slice(0, 40) : 'Halt'
    default:
      return step.id
  }
}

function stepSublabel(step: StepDef): string | undefined {
  switch (step.kind) {
    case 'tool_call':
      return step.id !== step.tool ? step.id : undefined
    case 'agent_step':
      return step.id
    case 'llm_step':
      return step.id
    case 'condition':
      return step.id
    case 'state':
      return step.id
    default:
      return undefined
  }
}

function addStep(
  ctx: LayoutContext,
  step: StepDef,
  x: number,
  parentId?: string,
): string {
  const nodeId = `step-${step.id}`
  const runDetail = ctx.runSteps?.get(step.id)

  ctx.nodes.push({
    id: nodeId,
    type: 'stepNode',
    position: { x, y: ctx.y },
    data: {
      stepId: step.id,
      kind: step.kind,
      label: stepLabel(step),
      sublabel: stepSublabel(step),
      explanation: step.explanation,
      stepDef: step,
      runStatus: runDetail?.status as RunStatus | undefined,
      runDetail,
      fireSeq: ctx.fireSeq?.get(step.id),
    },
  })

  if (parentId) {
    ctx.edges.push({
      id: `${parentId}->${nodeId}`,
      source: parentId,
      target: nodeId,
      type: 'smoothstep',
      animated: runDetail?.status === 'running',
      style: { stroke: '#475569', strokeWidth: 1.5 },
    })
  }

  ctx.y += NODE_H + GAP_Y

  if (step.kind === 'condition') {
    const branchY = ctx.y
    const thenSteps = step.then || []
    const elseSteps = step.else || []

    let lastThen = nodeId
    if (thenSteps.length > 0) {
      ctx.y = branchY
      for (const s of thenSteps) {
        const prev = lastThen
        lastThen = addStep(ctx, s, x - GAP_X / 2, prev)
      }
      ctx.edges[ctx.edges.length - thenSteps.length]!.label = 'yes'
    }

    let lastElse = nodeId
    const elseY = ctx.y
    if (elseSteps.length > 0) {
      ctx.y = branchY
      for (const s of elseSteps) {
        const prev = lastElse
        lastElse = addStep(ctx, s, x + GAP_X / 2, prev)
      }
      ctx.edges[ctx.edges.length - elseSteps.length]!.label = 'no'
    }

    ctx.y = Math.max(ctx.y, elseY)
    return thenSteps.length > 0 ? lastThen : lastElse
  }

  if (step.kind === 'loop' && step.body?.length) {
    let lastBody = nodeId
    for (const s of step.body) {
      lastBody = addStep(ctx, s, x, lastBody)
    }
    ctx.edges.push({
      id: `${lastBody}->loop-back-${nodeId}`,
      source: lastBody,
      target: nodeId,
      type: 'smoothstep',
      animated: true,
      style: { stroke: '#a855f7', strokeWidth: 1.5 },
      label: 'loop',
    })
    return lastBody
  }

  if (step.kind === 'parallel' && step.branches?.length) {
    const branchY = ctx.y
    const branchEnds: string[] = []
    const totalWidth = step.branches.length * GAP_X
    const startX = x - totalWidth / 2 + GAP_X / 2

    for (let i = 0; i < step.branches.length; i++) {
      ctx.y = branchY
      let lastInBranch = nodeId
      for (const s of step.branches[i]!) {
        lastInBranch = addStep(ctx, s, startX + i * GAP_X, lastInBranch)
      }
      branchEnds.push(lastInBranch)
    }

    const mergeId = `merge-${step.id}`
    ctx.nodes.push({
      id: mergeId,
      type: 'stepNode',
      position: { x, y: ctx.y },
      data: {
        stepId: `${step.id}-merge`,
        kind: 'parallel' as const,
        label: 'Merge',
        sublabel: (step as any).fan_in || 'all',
      },
    })
    for (const end of branchEnds) {
      ctx.edges.push({
        id: `${end}->${mergeId}`,
        source: end,
        target: mergeId,
        type: 'smoothstep',
        style: { stroke: '#475569', strokeWidth: 1.5 },
      })
    }
    ctx.y += NODE_H + GAP_Y
    return mergeId
  }

  return nodeId
}

export function buildGraph(
  def: PlaybookDef,
  runSteps?: StepRunDetail[],
  fireSeq?: Map<string, number>,
): { nodes: Node[]; edges: Edge[] } {
  const ctx: LayoutContext = {
    nodes: [],
    edges: [],
    y: 0,
    runSteps: runSteps ? new Map(runSteps.map((s) => [s.step_id, s])) : undefined,
    fireSeq,
  }

  const centerX = 400

  for (let i = 0; i < def.triggers.length; i++) {
    const trigger = def.triggers[i]!
    const triggerId = `trigger-${i}`
    ctx.nodes.push({
      id: triggerId,
      type: 'triggerNode',
      position: { x: centerX, y: ctx.y },
      data: {
        event: trigger.event,
        cron: trigger.cron,
        label: trigger.event || trigger.cron || 'Trigger',
      },
    })
    ctx.y += NODE_H + GAP_Y
  }

  let lastNodeId: string | undefined = ctx.nodes[ctx.nodes.length - 1]?.id

  for (const step of def.steps) {
    lastNodeId = addStep(ctx, step, centerX, lastNodeId)
  }

  return { nodes: ctx.nodes, edges: ctx.edges }
}
