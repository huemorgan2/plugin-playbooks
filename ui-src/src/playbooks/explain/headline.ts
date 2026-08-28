/**
 * plans/010: the panel's bottom-line headline, generated from the definition.
 *
 * Never sourced from `step.explanation` — agent prose can drift from the code;
 * the headline must be what the block actually does. Structural words only;
 * every variable part comes from the step's own fields.
 */

import type { StepDef } from '../types'

const trim = (s: string, n = 64) => (s.length > n ? `${s.slice(0, n - 1)}…` : s)

export function headline(step: StepDef): string {
  switch (step.kind) {
    case 'tool_call':
      return step.tool ? `Calls ${step.tool}` : `Runs ${step.id}`
    case 'code': {
      const n = (step.source ?? '').split('\n').filter((l) => l.trim()).length
      return n ? `Runs Python (${n} line${n === 1 ? '' : 's'})` : `Runs ${step.id}`
    }
    case 'condition':
      return step.when ? trim(`Branches on ${step.when}`) : `Branches`
    case 'loop': {
      if (step.over != null) {
        const src = typeof step.over === 'string' ? step.over : `${step.over.length} items`
        return trim(`For each ${step.item_name || 'item'} in ${src}`)
      }
      if (step.while) return trim(`Repeats while ${step.while}`)
      if (step.until) return trim(`Repeats until ${step.until}`)
      return 'Repeats its body'
    }
    case 'agent_step':
      return step.prompt ? trim(`Agent: ${step.prompt.replace(/\s+/g, ' ')}`) : `Runs the agent`
    case 'llm_step':
      return step.prompt ? trim(`Generate: ${step.prompt.replace(/\s+/g, ' ')}`) : `Calls the model`
    case 'parallel': {
      const n = step.branches?.length ?? 0
      return n ? `Runs ${n} branches at once` : 'Runs branches at once'
    }
    case 'wait_for_approval':
      return 'Waits for your approval'
    case 'wait_for_event':
      return step.event ? `Waits for ${step.event}` : 'Waits for an event'
    case 'subtask':
      return step.playbook ? `Runs playbook ${step.playbook}` : 'Runs a sub-playbook'
    case 'state': {
      const ops = step.state || []
      if (ops.length === 1) return trim(`${ops[0]!.op} ${ops[0]!.var}`)
      if (ops.length > 1) return `Updates ${ops.length} variables`
      return 'Updates run state'
    }
    case 'halt':
      return step.when ? trim(`Stops the run if ${step.when}`) : 'Stops the run'
    default:
      return step.id
  }
}
