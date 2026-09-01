// @vitest-environment jsdom
// plans/016 phase 3: the Plans tab — audit trail + autonomy switch.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'

vi.mock('../api', () => ({
  playbooksApi: {
    listPlans: vi.fn(),
    getPlan: vi.fn(),
    getSettings: vi.fn(),
    patchSettings: vi.fn(),
  },
}))

import { playbooksApi } from '../api'
import { PlansTab, factLine, renderInline } from '../PlansTab'

const api = playbooksApi as unknown as Record<string, ReturnType<typeof vi.fn>>

const BRIEF = {
  plan_id: 'p-1', title: 'Fix the greeter', status: 'proposed',
  playbook_refs: ['greeter'], created_at: '2026-08-30T10:00:00Z',
  updated_at: null, has_execution_summary: false,
}

const DETAIL = {
  ...BRIEF,
  status: 'approved',
  body: 'The greeter fails when no name is given. I will add a default.',
  rejection_note: null,
  execution_summary: 'Fixed and published without issues.',
  outcome_facts: [{
    action: 'promote', playbook: 'greeter', old_live_version: 1,
    new_live_version: 2, evidence_run_id: 'r-1', actor: 'owner',
    at: '2026-08-30T11:00:00Z',
  }],
  conversation_id: null,
}

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset?.()
  api.listPlans.mockResolvedValue({ plans: [BRIEF] })
  api.getSettings.mockResolvedValue({ plans_full_power: false })
  api.getPlan.mockResolvedValue(DETAIL)
})
afterEach(cleanup)

describe('PlansTab', () => {
  it('lists plans with a status pill, title, refs and headline count', async () => {
    render(<PlansTab agentName="Luna" />)
    await waitFor(() =>
      expect(screen.getByTestId('plans-headline').textContent).toBe('1 awaiting publish'))
    const row = screen.getByTestId('plan-row-p-1')
    expect(row.textContent).toContain('Fix the greeter')
    expect(row.textContent).toContain('greeter')
    const pill = screen.getByTestId('plan-status-pill')
    expect(pill.textContent).toBe('proposed')
    expect(pill.className).toContain('text-amber-300')
    expect(pill.className).not.toContain('bg-amber') // border+text only, never filled
  })

  it('says "No changes planned" when nothing is publishable', async () => {
    api.listPlans.mockResolvedValue({ plans: [{ ...BRIEF, status: 'done' }] })
    render(<PlansTab agentName="Luna" />)
    await waitFor(() =>
      expect(screen.getByTestId('plans-headline').textContent).toBe('No changes planned'))
  })

  it('expanding a row shows body, fact lines and the execution summary', async () => {
    render(<PlansTab agentName="Luna" />)
    await screen.findByTestId('plan-row-p-1')
    fireEvent.click(screen.getByText('Fix the greeter'))
    const detail = await screen.findByTestId('plan-detail')
    await waitFor(() => expect(detail.textContent).toContain('add a default'))
    const facts = screen.getByTestId('plan-facts')
    expect(facts.textContent).toContain('Published greeter v1 → v2 by you')
    expect(screen.getByTestId('plan-summary').textContent).toContain('without issues')
  })

  it('shows the rejection note on rejected plans', async () => {
    api.listPlans.mockResolvedValue({ plans: [{ ...BRIEF, status: 'rejected' }] })
    api.getPlan.mockResolvedValue({
      ...DETAIL, status: 'rejected', rejection_note: 'not this week',
      outcome_facts: [], execution_summary: null,
    })
    render(<PlansTab agentName="Luna" />)
    await screen.findByTestId('plan-row-p-1')
    fireEvent.click(screen.getByText('Fix the greeter'))
    const note = await screen.findByTestId('plan-rejection')
    expect(note.textContent).toContain('not this week')
  })

  it('autonomy switch states the mode and PATCHes plans_full_power', async () => {
    api.patchSettings.mockResolvedValue({ plans_full_power: true })
    render(<PlansTab agentName="Luna" />)
    await waitFor(() =>
      expect(screen.getByTestId('autonomy-headline').textContent).toBe('You approve each publish'))
    fireEvent.click(screen.getByTestId('autonomy-toggle'))
    await waitFor(() =>
      expect(api.patchSettings).toHaveBeenCalledWith({ plans_full_power: true }))
    await waitFor(() =>
      expect(screen.getByTestId('autonomy-headline').textContent).toContain('Full power'))
  })
})

describe('renderInline', () => {
  it('bolds **spans** and styles `code` instead of showing asterisks', () => {
    render(<p data-testid="ri">{renderInline('**Bad:** the `who` input is required')}</p>)
    const el = screen.getByTestId('ri')
    expect(el.textContent).toBe('Bad: the who input is required')
    expect(el.querySelector('strong')?.textContent).toBe('Bad:')
    expect(el.querySelector('code')?.textContent).toBe('who')
  })
})

describe('factLine', () => {
  it('renders short sentences, never JSON', () => {
    expect(factLine({
      action: 'restore', playbook: 'greeter', old_live_version: 3,
      new_live_version: 1, actor: 'owner',
    })).toBe('Restored greeter v3 → v1 by you')
    expect(factLine({
      action: 'publish', playbook: 'greeter', new_live_version: 1, actor: 'agent',
    })).toBe('Published greeter v1 by agent')
    expect(factLine({
      action: 'promote', playbook: 'greeter', old_live_version: 1,
      new_live_version: 2, actor: 'owner', test_run_forced: true,
    })).toContain('without a fresh test run')
  })
})
