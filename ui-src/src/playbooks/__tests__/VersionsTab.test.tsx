// @vitest-environment jsdom
import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import type { PlaybookDef, VersionDetail } from '../types'

vi.mock('../api', () => ({
  playbooksApi: {
    listVersions: vi.fn(),
    getVersion: vi.fn(),
    promoteVersion: vi.fn(),
    promoteCandidate: vi.fn(),
    listRuns: vi.fn().mockResolvedValue([]),
    getRun: vi.fn(),
  },
}))
vi.mock('../../lib/events', () => ({
  subscribePlaybookEvents: () => () => {},
}))
// Heavy siblings with their own fetches — not under test here.
vi.mock('../ManifestTab', () => ({ ManifestTab: () => <div data-testid="manifest-tab" /> }))
vi.mock('../TestsTab', () => ({ TestsTab: () => <div data-testid="tests-tab" /> }))
vi.mock('../RunsTab', () => ({ RunsTab: () => <div data-testid="runs-tab" /> }))

import { playbooksApi } from '../api'
import { VersionsTab, promoteRefusalMessage } from '../VersionsTab'

const api = playbooksApi as unknown as Record<string, ReturnType<typeof vi.fn>>

beforeAll(() => {
  class RO { observe() {} unobserve() {} disconnect() {} }
  ;(globalThis as any).ResizeObserver = RO
  ;(globalThis as any).DOMMatrixReadOnly = class { m22 = 1; constructor(_s?: string) {} }
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', { configurable: true, get: () => 500 })
  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', { configurable: true, get: () => 500 })
})
afterEach(cleanup)

const def: PlaybookDef = {
  name: 'greeter', display_name: 'Greeter', description: 'says hi',
  explanation: '', when_to_use: '', agent_autonomy: 'manual', triggers: [],
  steps: [{ id: 'a', kind: 'tool_call', tool: 'send_chat_message', args: { message: 'hi' } }],
}

function entry(
  version: number,
  extra: Partial<{ current: boolean; candidate: boolean; specs: { total: number; failed: number; green: number } }> = {},
) {
  return {
    version, title: `v${version} edit`, author: 'agent',
    created_at: '2026-08-30T10:00:00Z', runs: version, promoted_from: null,
    current: false, ...extra,
  }
}

function detailOf(version: number, live: boolean, candidate = false): VersionDetail {
  return {
    version, definition: def, code: null, manifest: `M${version}`, author: 'agent',
    message: `v${version} edit`, created_at: '2026-08-30T10:00:00Z',
    promoted_from: null, live, candidate, runs: version,
  }
}

function setup({ candidate = false, redV1 = false, requireSpecs = true }: { candidate?: boolean; redV1?: boolean; requireSpecs?: boolean } = {}) {
  api.listVersions.mockResolvedValue([
    ...(candidate ? [entry(3, { candidate: true })] : []),
    entry(2, { current: true, specs: { total: 2, failed: 0, green: 2 } }),
    entry(1, redV1 ? { specs: { total: 2, failed: 1, green: 1 } } : {}),
  ])
  api.getVersion.mockImplementation((_n: string, v: number) =>
    Promise.resolve(detailOf(v, v === 2, candidate && v === 3)),
  )
  const onPromoted = vi.fn()
  render(
    <VersionsTab
      name="greeter" agentName="Luna" liveVersion={2}
      candidateVersion={candidate ? 3 : null}
      onPromoted={onPromoted} onManifestSaved={() => {}} requireSpecs={requireSpecs}
    />,
  )
  return { onPromoted }
}

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset?.()
  api.listRuns.mockResolvedValue([])
})

describe('VersionsTab', () => {
  it('opens on the live version, highlighted, with the Published · Live badge and no "current"/"selected" words', async () => {
    setup()
    await screen.findByTestId('version-toolbar')
    expect(screen.getByTestId('toolbar-version').textContent).toBe('v2')
    const live = screen.getByTestId('version-row-2')
    expect(live.getAttribute('aria-current')).toBe('true')
    expect(live.className).toContain('bg-luna-600/20')
    expect(screen.getByTestId('version-row-1').getAttribute('aria-current')).toBeNull()
    const list = screen.getByTestId('version-list')
    expect(list.textContent).toContain('Published · Live')
    expect(list.textContent?.toLowerCase()).not.toContain('current')
    expect(list.textContent?.toLowerCase()).not.toContain('selected')
    // toolbar: badge, no promote button for the live version
    expect(screen.getAllByTestId('live-badge').length).toBeGreaterThanOrEqual(2)
    expect(screen.queryByTestId('promote-btn')).toBeNull()
  })

  it('selecting an older row shows Promote to live and promotes that version', async () => {
    const { onPromoted } = setup()
    await screen.findByTestId('version-toolbar')
    fireEvent.click(screen.getByTestId('version-row-1'))
    await waitFor(() => expect(screen.getByTestId('toolbar-version').textContent).toBe('v1'))
    expect(screen.getByTestId('version-row-1').getAttribute('aria-current')).toBe('true')
    const btn = screen.getByTestId('promote-btn')
    expect(btn.textContent).toContain('Promote to live')
    api.promoteVersion.mockResolvedValue({ name: 'greeter', version: 1, promoted_from: 2, status: 'published' })
    fireEvent.click(btn)
    await waitFor(() => expect(onPromoted).toHaveBeenCalledWith(1))
    expect(api.promoteVersion).toHaveBeenCalledWith('greeter', 1)
    expect(api.promoteCandidate).not.toHaveBeenCalled()
  })

  it('promoting the candidate row calls promoteCandidate', async () => {
    const { onPromoted } = setup({ candidate: true })
    await screen.findByTestId('version-toolbar')
    fireEvent.click(screen.getByTestId('version-row-3'))
    await waitFor(() => expect(screen.getByTestId('toolbar-version').textContent).toBe('v3'))
    api.promoteCandidate.mockResolvedValue({ name: 'greeter', live_version: 3, promoted_from: 2, status: 'published' })
    fireEvent.click(screen.getByTestId('promote-btn'))
    await waitFor(() => expect(onPromoted).toHaveBeenCalledWith(3))
    expect(api.promoteCandidate).toHaveBeenCalledWith('greeter')
    expect(api.promoteVersion).not.toHaveBeenCalled()
  })

  it('a 422 refusal is shown under the toolbar, naming the gate', async () => {
    const { onPromoted } = setup()
    await screen.findByTestId('version-toolbar')
    fireEvent.click(screen.getByTestId('version-row-1'))
    await screen.findByTestId('promote-btn')
    api.promoteVersion.mockRejectedValue(new Error(
      '422: {"detail":{"gate":"test_run","error":"version 1 has never completed a run"}}',
    ))
    fireEvent.click(screen.getByTestId('promote-btn'))
    const err = await screen.findByTestId('promote-error')
    expect(err.textContent).toContain('never completed a run')
    expect(onPromoted).not.toHaveBeenCalled()
  })

  it('view switch: Code / Manifest / Tests / Runs render per version', async () => {
    setup()
    await screen.findByTestId('version-toolbar')
    fireEvent.click(screen.getByTestId('view-code'))
    expect(await screen.findByTestId('code-view')).toBeTruthy()
    fireEvent.click(screen.getByTestId('view-manifest'))
    expect(await screen.findByTestId('manifest-tab')).toBeTruthy()   // live → editable
    fireEvent.click(screen.getByTestId('version-row-1'))
    expect(await screen.findByTestId('manifest-snapshot')).toBeTruthy()  // older → snapshot
    fireEvent.click(screen.getByTestId('view-tests'))
    expect(await screen.findByTestId('tests-tab')).toBeTruthy()
    fireEvent.click(screen.getByTestId('view-runs'))
    expect(await screen.findByTestId('runs-tab')).toBeTruthy()
  })
})

describe('VersionsTab — per-version tests (phase 5)', () => {
  it('rows show that version\'s test counts and a red version cannot be promoted', async () => {
    setup({ redV1: true })
    await screen.findByTestId('version-toolbar')
    expect(screen.getByTestId('version-specs-2').textContent).toBe('2 tests · 2 green')
    expect(screen.getByTestId('version-specs-1').textContent).toBe('2 tests · 1 red')
    fireEvent.click(screen.getByTestId('version-row-1'))
    const btn = await screen.findByTestId('promote-btn')
    expect((btn as HTMLButtonElement).disabled).toBe(true)
    expect(btn.getAttribute('title')).toContain('red')
  })

  // plans/016 phase 6: the client-side red-disable follows the specs gate switch.
  it('a red version can be promoted when the specs gate is off in Settings → Publish', async () => {
    setup({ redV1: true, requireSpecs: false })
    await screen.findByTestId('version-toolbar')
    expect(screen.getByTestId('version-specs-1').textContent).toBe('2 tests · 1 red')
    fireEvent.click(screen.getByTestId('version-row-1'))
    const btn = await screen.findByTestId('promote-btn')
    expect((btn as HTMLButtonElement).disabled).toBe(false)
  })
})

describe('promoteRefusalMessage', () => {
  it('prefers message, then error, then gate', () => {
    expect(promoteRefusalMessage(new Error('422: {"detail":{"message":"m","error":"e"}}'))).toBe('m')
    expect(promoteRefusalMessage(new Error('422: {"detail":{"error":"e"}}'))).toBe('e')
    expect(promoteRefusalMessage(new Error('422: {"detail":{"gate":"specs"}}'))).toContain("'specs'")
    expect(promoteRefusalMessage(new Error('boom'))).toBe('boom')
  })
})
