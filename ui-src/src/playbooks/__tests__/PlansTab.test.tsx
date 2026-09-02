// @vitest-environment jsdom
// plans/016 phase 3: the Plans tab — audit trail + autonomy switch.
// plans/022: table list, owner status control, full-page reader, delete.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'

vi.mock('../api', () => ({
  playbooksApi: {
    listPlans: vi.fn(),
    getPlan: vi.fn(),
    patchPlan: vi.fn(),
    deletePlan: vi.fn(),
    getSettings: vi.fn(),
    patchSettings: vi.fn(),
  },
}))

import { playbooksApi } from '../api'
import { PlansTab, PlansList, factLine, renderInline } from '../PlansTab'

const api = playbooksApi as unknown as Record<string, ReturnType<typeof vi.fn>>

const BRIEF = {
  plan_id: 'p-1', title: 'Fix the greeter', status: 'proposed',
  playbook_refs: ['greeter'], created_at: '2026-08-30T10:00:00Z',
  updated_at: '2026-08-30T10:30:00Z', has_execution_summary: false,
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
  api.patchPlan.mockResolvedValue({ ...BRIEF, status: 'done' })
  api.deletePlan.mockResolvedValue({ plan_id: 'p-1', deleted: true })
})
afterEach(cleanup)

describe('PlansTab', () => {
  it('lists plans as a table with title, status, refs, dates and headline', async () => {
    render(<PlansTab agentName="Luna" />)
    await waitFor(() =>
      expect(screen.getByTestId('plans-headline').textContent).toBe('1 awaiting publish'))
    const table = screen.getByTestId('plans-table')
    expect(table.textContent).toContain('Plan')
    expect(table.textContent).toContain('Status')
    expect(table.textContent).toContain('Playbooks')
    expect(table.textContent).toContain('Created')
    expect(table.textContent).toContain('Updated')
    const row = screen.getByTestId('plan-row-p-1')
    expect(row.textContent).toContain('Fix the greeter')
    expect(row.textContent).toContain('greeter')
    expect(row.textContent).toContain('Aug 30') // created + updated dates
    const select = screen.getByTestId('plan-status-select') as HTMLSelectElement
    expect(select.value).toBe('proposed')
    expect(select.className).toContain('text-amber-300')
    expect(select.className).not.toContain('bg-amber') // border+text only, never filled
  })

  it('says "No changes planned" when nothing is publishable', async () => {
    api.listPlans.mockResolvedValue({ plans: [{ ...BRIEF, status: 'done' }] })
    render(<PlansTab agentName="Luna" />)
    await waitFor(() =>
      expect(screen.getByTestId('plans-headline').textContent).toBe('No changes planned'))
  })

  it('changing the status in a row PATCHes the plan optimistically', async () => {
    render(<PlansTab agentName="Luna" />)
    await screen.findByTestId('plan-row-p-1')
    const select = screen.getByTestId('plan-status-select') as HTMLSelectElement
    fireEvent.change(select, { target: { value: 'done' } })
    await waitFor(() => expect(api.patchPlan).toHaveBeenCalledWith('p-1', 'done'))
    expect((screen.getByTestId('plan-status-select') as HTMLSelectElement).value).toBe('done')
    // no navigation happened — still the table
    expect(screen.getByTestId('plans-table')).toBeTruthy()
  })

  it('deleting a row asks for an in-place confirm, then DELETEs it', async () => {
    render(<PlansTab agentName="Luna" />)
    await screen.findByTestId('plan-row-p-1')
    fireEvent.click(screen.getByTestId('plan-delete-p-1'))
    expect(api.deletePlan).not.toHaveBeenCalled() // armed, not yet deleted
    fireEvent.click(screen.getByTestId('plan-delete-p-1-confirm'))
    await waitFor(() => expect(api.deletePlan).toHaveBeenCalledWith('p-1'))
    await waitFor(() => expect(screen.queryByTestId('plan-row-p-1')).toBeNull())
  })

  it('clicking a row opens the reader: body, facts, summary — and back returns', async () => {
    render(<PlansTab agentName="Luna" />)
    await screen.findByTestId('plan-row-p-1')
    fireEvent.click(screen.getByText('Fix the greeter'))
    const reader = await screen.findByTestId('plan-reader')
    await waitFor(() =>
      expect(screen.getByTestId('plan-body').textContent).toContain('add a default'))
    expect(reader.textContent).toContain('Fix the greeter')
    expect(screen.getByTestId('plan-facts').textContent)
      .toContain('Published greeter v1 → v2 by you')
    expect(screen.getByTestId('plan-summary').textContent).toContain('without issues')
    fireEvent.click(screen.getByTestId('plan-back'))
    expect(screen.getByTestId('plans-table')).toBeTruthy()
  })

  it('the reader can change status and delete the plan', async () => {
    render(<PlansTab agentName="Luna" />)
    await screen.findByTestId('plan-row-p-1')
    fireEvent.click(screen.getByText('Fix the greeter'))
    await screen.findByTestId('plan-body')
    const select = screen.getByTestId('plan-status-select') as HTMLSelectElement
    expect(select.value).toBe('approved')
    fireEvent.change(select, { target: { value: 'rejected' } })
    await waitFor(() => expect(api.patchPlan).toHaveBeenCalledWith('p-1', 'rejected'))
    fireEvent.click(screen.getByTestId('plan-delete'))
    fireEvent.click(screen.getByTestId('plan-delete-confirm'))
    await waitFor(() => expect(api.deletePlan).toHaveBeenCalledWith('p-1'))
    // deleting from the reader navigates back to the (now empty) list
    await waitFor(() => expect(screen.getByTestId('plans-headline')).toBeTruthy())
  })

  it('a done plan shows the locked note in the reader', async () => {
    api.getPlan.mockResolvedValue({ ...DETAIL, status: 'done' })
    render(<PlansTab agentName="Luna" />)
    await screen.findByTestId('plan-row-p-1')
    fireEvent.click(screen.getByText('Fix the greeter'))
    const note = await screen.findByTestId('plan-locked-note')
    expect(note.textContent).toContain('locked for Luna')
    expect(note.textContent).toContain('proposed')
  })

  it('shows the rejection note only on rejected plans', async () => {
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

// plans/021: the editor's per-playbook Plans tab reuses PlansList with a
// `playbook` filter passed through to the API.
describe('PlansList (per-playbook)', () => {
  it('passes the playbook filter to listPlans', async () => {
    render(<PlansList agentName="Luna" playbook="greeter" />)
    await waitFor(() =>
      expect(api.listPlans).toHaveBeenCalledWith(undefined, 'greeter'))
    expect(api.getSettings).not.toHaveBeenCalled() // no autonomy switch here
  })

  it('shows a per-playbook empty state', async () => {
    api.listPlans.mockResolvedValue({ plans: [] })
    render(<PlansList agentName="Luna" playbook="greeter" />)
    await waitFor(() =>
      expect(screen.getByTestId('plans-headline').textContent).toBe('No plans yet'))
    expect(screen.getByText(/change this playbook/).textContent)
      .toContain('Ask Luna to change this playbook')
  })

  it('unfiltered PlansTab still lists all plans and keeps the switch', async () => {
    render(<PlansTab agentName="Luna" />)
    await waitFor(() =>
      expect(api.listPlans).toHaveBeenCalledWith(undefined, undefined))
    expect(screen.getByTestId('autonomy-toggle')).toBeTruthy()
  })
})
