// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { PublishSettings } from '../PublishSettings'

afterEach(cleanup)

describe('PublishSettings', () => {
  it('renders both Luna-style switches from the value', () => {
    render(<PublishSettings value={{ require_specs: true, require_run: false }} onChange={() => {}} />)
    expect(screen.getByText('Publish / Promote settings')).toBeTruthy()
    const specs = screen.getByTestId('switch-require-specs')
    const run = screen.getByTestId('switch-require-run')
    expect(specs.getAttribute('aria-checked')).toBe('true')
    expect(run.getAttribute('aria-checked')).toBe('false')
    expect(specs.className).toContain('w-10 h-5 rounded-full')
    expect(specs.className).toContain('bg-emerald-600')
    expect(run.className).toContain('bg-ink-700')
    expect(screen.getByText('Pushing a version requires all tests to be green')).toBeTruthy()
    expect(screen.getByText('Pushing a version requires at least one successful run')).toBeTruthy()
  })

  it('click reports the flipped flag only', () => {
    const onChange = vi.fn()
    render(<PublishSettings value={{ require_specs: true, require_run: true }} onChange={onChange} />)
    fireEvent.click(screen.getByTestId('switch-require-specs'))
    expect(onChange).toHaveBeenCalledWith({ require_specs: false })
    fireEvent.click(screen.getByTestId('switch-require-run'))
    expect(onChange).toHaveBeenCalledWith({ require_run: false })
  })
})
