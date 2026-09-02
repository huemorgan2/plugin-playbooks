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
import { VersionsTab, promoteRefusalMessage, promoteRefusalGate } from '../VersionsTab'

const api = playbooksApi as unknown as Record<string, ReturnType<typeof vi.fn>>

// 021: the Promote click opens a ✓/✗ confirm; the confirm button publishes.
async function promoteViaConfirm() {
  fireEvent.click(screen.getByTestId('promote-btn'))
  await screen.findByTestId('promote-confirm')
  fireEvent.click(screen.getByTestId('promote-confirm-btn'))
}

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

function setup({ candidate = false, redV1 = false }: { candidate?: boolean; redV1?: boolean } = {}) {
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
      onPromoted={onPromoted} onManifestSaved={() => {}}
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
    api.promoteVersion.mockResolvedValue({ name: 'greeter', live_version: 1, promoted_from: 2, status: 'promoted' })
    await promoteViaConfirm()
    await waitFor(() => expect(onPromoted).toHaveBeenCalledWith(1))
    expect(api.promoteVersion).toHaveBeenCalledWith('greeter', 1)
    expect(api.promoteCandidate).not.toHaveBeenCalled()
  })

  it('promoting the candidate row calls promoteCandidate', async () => {
    const { onPromoted } = setup({ candidate: true })
    await screen.findByTestId('version-toolbar')
    fireEvent.click(screen.getByTestId('version-row-3'))
    await waitFor(() => expect(screen.getByTestId('toolbar-version').textContent).toBe('v3'))
    api.promoteCandidate.mockResolvedValue({ name: 'greeter', live_version: 3, promoted_from: 2, status: 'promoted' })
    await promoteViaConfirm()
    await waitFor(() => expect(onPromoted).toHaveBeenCalledWith(3))
    expect(api.promoteCandidate).toHaveBeenCalledWith('greeter')
    expect(api.promoteVersion).not.toHaveBeenCalled()
  })

  it('a 422 refusal is shown under the toolbar', async () => {
    const { onPromoted } = setup()
    await screen.findByTestId('version-toolbar')
    fireEvent.click(screen.getByTestId('version-row-1'))
    await screen.findByTestId('promote-btn')
    api.promoteVersion.mockRejectedValue(new Error(
      '422: {"detail":{"gate":"probes","message":"Promote refused — a tool this playbook uses is broken: send_chat_message — dead"}}',
    ))
    await promoteViaConfirm()
    const err = await screen.findByTestId('promote-error')
    expect(err.textContent).toContain('broken')
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

describe('VersionsTab — the ✓/✗ promote confirm (021)', () => {
  it('a candidate with no tests: button stays enabled, confirm shows ✗ and Publish anyway', async () => {
    setup({ candidate: true })
    await screen.findByTestId('version-toolbar')
    // v3 candidate: no specs cache → ✗ "No tests defined", but never disabled
    fireEvent.click(screen.getByTestId('version-row-3'))
    const btn = await screen.findByTestId('promote-btn')
    expect((btn as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(btn)
    const confirm = await screen.findByTestId('promote-confirm')
    expect(confirm.textContent).toContain('✗')
    expect(screen.getByTestId('promote-confirm-btn').textContent).toBe('Publish anyway')
  })

  it('a red version still promotes — Publish anyway, owner click is the consent', async () => {
    const { onPromoted } = setup({ redV1: true })
    await screen.findByTestId('version-toolbar')
    expect(screen.getByTestId('version-specs-2').textContent).toBe('2 tests · 2 green')
    expect(screen.getByTestId('version-specs-1').textContent).toBe('2 tests · 1 red')
    fireEvent.click(screen.getByTestId('version-row-1'))
    const btn = await screen.findByTestId('promote-btn')
    expect((btn as HTMLButtonElement).disabled).toBe(false)
    api.promoteVersion.mockResolvedValue({ name: 'greeter', live_version: 1, promoted_from: 2, status: 'promoted' })
    fireEvent.click(btn)
    const confirm = await screen.findByTestId('promote-confirm')
    expect(confirm.textContent).toContain('1 of 2 tests red')
    const go = screen.getByTestId('promote-confirm-btn')
    expect(go.textContent).toBe('Publish anyway')
    fireEvent.click(go)
    await waitFor(() => expect(onPromoted).toHaveBeenCalledWith(1))
    expect(api.promoteVersion).toHaveBeenCalledWith('greeter', 1)
  })

  it('a green version with runs shows all-✓ and a plain Publish button', async () => {
    api.listVersions.mockResolvedValue([
      entry(2, { current: true, specs: { total: 2, failed: 0, green: 2 } }),
      entry(1, { specs: { total: 3, failed: 0, green: 3 } }),
    ])
    api.getVersion.mockImplementation((_n: string, v: number) =>
      Promise.resolve(detailOf(v, v === 2)),
    )
    render(
      <VersionsTab
        name="greeter" agentName="Luna" liveVersion={2} candidateVersion={null}
        onPromoted={() => {}} onManifestSaved={() => {}}
      />,
    )
    await screen.findByTestId('version-toolbar')
    fireEvent.click(screen.getByTestId('version-row-1'))
    const btn = await screen.findByTestId('promote-btn')
    fireEvent.click(btn)
    const confirm = await screen.findByTestId('promote-confirm')
    expect(confirm.textContent).toContain('Tests: 3/3 green')
    expect(confirm.textContent).toContain('Has run 1 time')
    expect(confirm.textContent).not.toContain('✗')
    expect(screen.getByTestId('promote-confirm-btn').textContent).toBe('Publish')
  })

  it('version list folds to a slim rail and back; toolbar tabs stay present', async () => {
    setup()
    await screen.findByTestId('version-toolbar')
    fireEvent.click(screen.getByTestId('versions-collapse'))
    expect(screen.queryByTestId('version-list')).toBeNull()
    expect(screen.getByTestId('version-list-collapsed')).toBeTruthy()
    // the view tabs never disappear with the list open or closed
    for (const v of ['canvas', 'code', 'manifest', 'tests', 'runs']) {
      expect(screen.getByTestId(`view-${v}`)).toBeTruthy()
    }
    fireEvent.click(screen.getByTestId('versions-expand'))
    expect(screen.getByTestId('version-list')).toBeTruthy()
    expect(screen.queryByTestId('version-list-collapsed')).toBeNull()
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

describe('promoteRefusalGate', () => {
  it('extracts the gate name, null otherwise', () => {
    expect(promoteRefusalGate(new Error('422: {"detail":{"gate":"test_run"}}'))).toBe('test_run')
    expect(promoteRefusalGate(new Error('boom'))).toBeNull()
  })
})
