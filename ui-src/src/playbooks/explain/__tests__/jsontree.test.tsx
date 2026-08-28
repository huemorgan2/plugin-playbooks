// @vitest-environment jsdom
/**
 * plans/010 phase 3: JsonTree render tests — collapsible typed tree for run
 * raw inputs/outputs. Zero-data rule: every token on screen comes from the
 * data prop.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { render, cleanup, fireEvent } from '@testing-library/react'
import { JsonTree } from '../jsontree'

afterEach(cleanup)

describe('JsonTree', () => {
  it('renders keys and typed scalar values from the data', () => {
    const { container } = render(
      <JsonTree data={{ name: 'roy', count: 3, ok: true, missing: null }} />,
    )
    const text = container.textContent!
    expect(text).toContain('name')
    expect(text).toContain('"roy"')
    expect(text).toContain('count')
    expect(text).toContain('3')
    expect(text).toContain('ok')
    expect(text).toContain('true')
    expect(text).toContain('missing')
    expect(text).toContain('null')
  })

  it('renders nothing beyond the data — no sample tokens', () => {
    const { container } = render(<JsonTree data={{ a: 1 }} />)
    // Only the key, colon, value and structural brackets appear.
    expect(container.textContent!.replace(/\s/g, '')).toBe('{a:1}')
  })

  it('deep nodes start collapsed with a count summary and expand on click', () => {
    const { container } = render(
      <JsonTree data={{ outer: { inner: { leaf: 'deep-value' } } }} />,
    )
    expect(container.textContent).not.toContain('deep-value')
    expect(container.textContent).toContain('1 key')
    const innerToggle = container
      .querySelector('[data-json-key="inner"]')!
      .querySelector('button')!
    fireEvent.click(innerToggle)
    expect(container.textContent).toContain('deep-value')
  })

  it('renders arrays with index keys and item counts when collapsed', () => {
    const { container } = render(
      <JsonTree data={{ items: ['x', 'y'] }} />,
    )
    const text = container.textContent!
    expect(text).toContain('"x"')
    expect(text).toContain('"y"')
    const itemsToggle = container
      .querySelector('[data-json-key="items"]')!
      .querySelector('button')!
    fireEvent.click(itemsToggle)
    expect(container.textContent).toContain('2 items')
    expect(container.textContent).not.toContain('"x"')
  })
})
