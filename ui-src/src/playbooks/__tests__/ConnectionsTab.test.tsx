// @vitest-environment jsdom
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'

vi.mock('../api', () => ({
  playbooksApi: {
    getProbes: vi.fn(),
    runPreflight: vi.fn(),
  },
}))

import { playbooksApi } from '../api'
import { ConnectionsTab } from '../ConnectionsTab'

const api = playbooksApi as unknown as Record<string, ReturnType<typeof vi.fn>>
afterEach(cleanup)

describe('ConnectionsTab (0.47.0 — probes only, the specs half is gone)', () => {
  it('fetches the probes, headlines them and re-checks on click', async () => {
    api.getProbes.mockResolvedValue({ name: 'greeter', probes: [
      { tool: 'send_chat_message', status: 'ok', failure_class: null, detail: null, probed_at: null },
      { tool: 'gmail_send', status: 'failed', failure_class: 'credential_dead', detail: null, probed_at: null },
    ] })
    api.runPreflight.mockResolvedValue({ name: 'greeter' })
    render(<ConnectionsTab name="greeter" />)
    expect((await screen.findByTestId('connections-header')).textContent).toBe('Connections')
    expect(api.getProbes).toHaveBeenCalledWith('greeter')
    expect(screen.getByTestId('probes-headline').textContent).toBe('1 tool broken')
    expect(screen.getAllByTestId('probe-row')).toHaveLength(2)
    expect(screen.getByText('credential dead')).toBeTruthy()
    fireEvent.click(screen.getByTestId('probes-check-now'))
    await waitFor(() => expect(api.runPreflight).toHaveBeenCalledWith('greeter'))
    expect(api.getProbes).toHaveBeenCalledTimes(2)
  })

  it('renders nothing test-shaped: no stored tests, no Run all', async () => {
    api.getProbes.mockResolvedValue({ name: 'greeter', probes: [] })
    render(<ConnectionsTab name="greeter" />)
    await screen.findByTestId('connections-header')
    expect(screen.queryByTestId('tests-header')).toBeNull()
    expect(screen.queryByTestId('specs-run-all')).toBeNull()
    expect(screen.getByTestId('probes-headline').textContent).toBe('Nothing verified yet')
  })
})
