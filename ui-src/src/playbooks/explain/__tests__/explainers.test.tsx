// @vitest-environment jsdom
/**
 * plans/010: zero-data guarantee for the explain renderers.
 * Every data token on screen must exist in the fixture definition, and an
 * absent field must leave no trace (no placeholder standing in for content).
 */
import { describe, it, expect, afterEach } from 'vitest'
import { render, cleanup } from '@testing-library/react'
import type { StepDef } from '../../types'
import { STEP_EXPLAINERS } from '../registry'
import { KVTable, SchemaTree, Value } from '../primitives'

afterEach(cleanup)

function renderKind(step: StepDef) {
  const Explainer = STEP_EXPLAINERS[step.kind]!
  expect(Explainer).toBeTruthy()
  return render(<Explainer step={step} />)
}

describe('tool_call explainer', () => {
  it('shows the tool name and every arg key/value from the definition', () => {
    const { container } = renderKind({
      id: 's1', kind: 'tool_call', tool: 'web_search',
      args: { query: '{{ inputs.topic }}', limit: 5 },
    })
    const text = container.textContent!
    expect(text).toContain('web_search')
    expect(text).toContain('query')
    expect(text).toContain('inputs.topic')
    expect(text).toContain('limit')
    expect(text).toContain('5')
  })

  it('renders no args section when args are absent', () => {
    const { container } = renderKind({ id: 's1', kind: 'tool_call', tool: 'web_search' })
    expect(container.textContent).not.toContain('Arguments')
  })
})

describe('code explainer', () => {
  it('shows the full source and code_inputs', () => {
    const source = 'def run(inputs):\n    return inputs["a"] + 1'
    const { container } = renderKind({
      id: 'calc', kind: 'code', source,
      code_inputs: { a: '{{ steps.prev.result }}' },
    })
    const text = container.textContent!
    expect(text).toContain('def')
    expect(text).toContain('return')
    expect(text).toContain('inputs')
    expect(text).toContain('steps.prev.result')
    expect(text).toContain('steps.calc.result') // derived from the step id
    expect(container.querySelector('[data-testid="explain-code"]')).toBeTruthy()
  })

  it('renders no source block when source is absent', () => {
    const { container } = renderKind({ id: 'calc', kind: 'code' })
    expect(container.querySelector('[data-testid="explain-code"]')).toBeNull()
    expect(container.textContent).not.toContain('Source')
  })
})

describe('KVTable', () => {
  it('renders nested objects and typed literals', () => {
    const { container } = render(
      <KVTable data={{ n: 3, on: true, none: null, obj: { inner: 'deep' } }} />,
    )
    const text = container.textContent!
    expect(text).toContain('3')
    expect(text).toContain('true')
    expect(text).toContain('null')
    expect(text).toContain('inner')
    expect(text).toContain('deep')
  })

  it('renders nothing for an empty object', () => {
    const { container } = render(<KVTable data={{}} />)
    expect(container.textContent).toBe('')
  })
})

describe('Value', () => {
  it('treats template strings as expressions, plain strings as quoted literals', () => {
    const tpl = render(<Value value="{{ vars.count }}" />)
    expect(tpl.container.querySelector('[data-expr]')).toBeTruthy()
    const lit = render(<Value value="hello" />)
    expect(lit.container.textContent).toBe('"hello"')
  })
})

describe('SchemaTree', () => {
  it('renders field names, types, required and descriptions from the schema', () => {
    const { container } = render(
      <SchemaTree schema={{
        type: 'object',
        required: ['title'],
        properties: {
          title: { type: 'string', description: 'The headline' },
          tags: { type: 'array', items: { type: 'string' } },
        },
      }} />,
    )
    const text = container.textContent!
    expect(text).toContain('title')
    expect(text).toContain('string')
    expect(text).toContain('The headline')
    expect(text).toContain('array<string>')
    expect(container.querySelectorAll('[data-schema-field]')).toHaveLength(2)
  })

  it('renders nothing without properties', () => {
    const { container } = render(<SchemaTree schema={{ type: 'object' }} />)
    expect(container.textContent).toBe('')
  })
})
