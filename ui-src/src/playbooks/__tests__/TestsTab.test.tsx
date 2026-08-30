// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'

vi.mock('../api', () => ({
  playbooksApi: {
    getSpecs: vi.fn(),
    getProbes: vi.fn().mockResolvedValue({ name: 'greeter', probes: [] }),
    runSpecs: vi.fn(),
    runPreflight: vi.fn(),
  },
}))
vi.mock('../agentIdentity', () => ({ useAgentName: () => 'Luna' }))

import { playbooksApi } from '../api'
import { TestsTab } from '../TestsTab'

const api = playbooksApi as unknown as Record<string, ReturnType<typeof vi.fn>>
afterEach(cleanup)

describe('TestsTab (per version, phase 5)', () => {
  it('fetches, titles and runs the selected version\'s tests', async () => {
    api.getSpecs.mockResolvedValue({ name: 'greeter', version: 7, specs: [
      { name: 's1', spec: {}, created_by: 'agent', last_result: { passed: true }, last_run_at: null, last_version: 7, updated_at: null },
    ] })
    api.runSpecs.mockResolvedValue({ name: 'greeter', ran_against_version: 7, total: 1, passed: 1, failed: 0, results: [] })
    render(<TestsTab name="greeter" version={7} />)
    expect((await screen.findByTestId('tests-header')).textContent).toBe('Tests of v7')
    expect(api.getSpecs).toHaveBeenCalledWith('greeter', 7)
    fireEvent.click(screen.getByTestId('specs-run-all'))
    await waitFor(() => expect(api.runSpecs).toHaveBeenCalledWith('greeter', 7))
  })
})
