/**
 * plans/010 phase 3: drift guard between definition.py and the explain UI.
 *
 * stepdef_fields.json is exported from the backend StepDef by
 * tests/test_stepdef_fields_export.py (wire names). Every field must be
 * either CONSUMED by the explain surface (headline, explainers, dataflow,
 * footer, panel chrome) or listed in IGNORED with a reason. A new backend
 * field fails this test until the UI decides what to do with it — exactly
 * the class of bug where `code.source` (pre-plan) and `timeout` (phase 2)
 * silently never rendered.
 */
import { describe, it, expect } from 'vitest'
import exported from '../stepdef_fields.json'

// Fields the explain surface renders (somewhere: headline.ts, registry.tsx,
// dataflow.ts, or the panel itself). Keep in sync deliberately — adding a
// field here is a claim that the UI shows it.
const CONSUMED = new Set([
  'id', 'kind', 'explanation',                         // panel chrome
  'tool', 'args',                                      // tool_call
  'prompt', 'output_schema', 'tools',                  // agent_step
  'purpose', 'model', 'system',                        // llm_step
  'when', 'then', 'else',                              // condition (+halt guard)
  'branches', 'fan_in',                                // parallel
  'show',                                              // wait_for_approval
  'event', 'event_filter', 'timeout_seconds',          // wait_for_event
  'playbook', 'inputs_map', 'returns',                 // subtask
  'over', 'body', 'max_iterations', 'until', 'while',  // loop
  'break_when', 'concurrency', 'item_name', 'collect', // loop
  'state',                                             // state
  'source', 'code_inputs',                             // code
  'value',                                             // halt / state op values
  'retry', 'on_error', 'timeout',                      // footer chips
])

// Fields deliberately not rendered, with the reason on record.
const IGNORED = new Map<string, string>([
  // (none today — every StepDef field renders somewhere)
])

describe('StepDef field drift', () => {
  it('every backend field is consumed or explicitly ignored', () => {
    const missing = exported.fields.filter(
      (f: string) => !CONSUMED.has(f) && !IGNORED.has(f),
    )
    expect(missing, [
      'definition.py grew StepDef fields the explain UI does not handle:',
      missing.join(', '),
      'Render them (then add to CONSUMED) or add to IGNORED with a reason.',
    ].join(' ')).toEqual([])
  })

  it('CONSUMED and IGNORED contain no stale fields', () => {
    const fields = new Set<string>(exported.fields)
    const staleConsumed = [...CONSUMED].filter((f) => !fields.has(f))
    const staleIgnored = [...IGNORED.keys()].filter((f) => !fields.has(f))
    expect(staleConsumed, `CONSUMED lists fields no longer in StepDef: ${staleConsumed}`).toEqual([])
    expect(staleIgnored, `IGNORED lists fields no longer in StepDef: ${staleIgnored}`).toEqual([])
  })

  it('CONSUMED and IGNORED do not overlap', () => {
    const overlap = [...CONSUMED].filter((f) => IGNORED.has(f))
    expect(overlap).toEqual([])
  })
})
