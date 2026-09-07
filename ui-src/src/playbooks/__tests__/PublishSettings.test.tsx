// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import { PublishSettings } from '../PublishSettings'

afterEach(cleanup)

describe('PublishSettings', () => {
  it('renders the single Luna-style switch from the value', () => {
    render(<PublishSettings value={{ require_run: false }} onChange={() => {}} />)
    expect(screen.getByText('Publish / Promote settings')).toBeTruthy()
    const run = screen.getByTestId('switch-require-run')
    expect(run.getAttribute('aria-checked')).toBe('false')
    expect(run.className).toContain('w-10 h-5 rounded-full')
    expect(run.className).toContain('bg-ink-700')
    expect(screen.getByText('Pushing a version requires at least one successful run')).toBeTruthy()
    // 0.47.0: the "all tests green" switch went with the specs feature.
    expect(screen.queryByTestId('switch-require-specs')).toBeNull()
    expect(screen.getAllByRole('switch')).toHaveLength(1)
  })

  it('click reports the flipped flag', () => {
    const onChange = vi.fn()
    render(<PublishSettings value={{ require_run: true }} onChange={onChange} />)
    const run = screen.getByTestId('switch-require-run')
    expect(run.className).toContain('bg-emerald-600')
    fireEvent.click(run)
    expect(onChange).toHaveBeenCalledWith({ require_run: false })
  })
})
