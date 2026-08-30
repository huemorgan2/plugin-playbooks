// plans/016 phase 6: "Publish / Promote settings" — the owner-switchable
// publish gates. Rendered inside SettingsTab. Off = the gate still runs and
// is reported on the version, but never refuses a promote.
import { cn } from '../lib/cn'

export interface PublishSettingsValue {
  require_specs: boolean
  require_run: boolean
}

export function Switch({
  on,
  onToggle,
  label,
  testId,
}: {
  on: boolean
  onToggle: () => void
  label: string
  testId?: string
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      data-testid={testId}
      onClick={onToggle}
      className={cn(
        'relative w-10 h-5 rounded-full transition-colors shrink-0',
        on ? 'bg-emerald-600' : 'bg-ink-700',
      )}
    >
      <div className={cn(
        'absolute top-0.5 w-4 h-4 rounded-full bg-white transition-transform',
        on ? 'left-[22px]' : 'left-0.5',
      )} />
    </button>
  )
}

const ROWS: { key: keyof PublishSettingsValue; label: string; hint: string; testId: string }[] = [
  {
    key: 'require_specs',
    label: 'Pushing a version requires all tests to be green',
    hint: 'The tests of the version being pushed run first; one red test refuses the promote.',
    testId: 'switch-require-specs',
  },
  {
    key: 'require_run',
    label: 'Pushing a version requires at least one successful run',
    hint: 'A candidate needs a green test run since its edit; a restore needs one green run ever.',
    testId: 'switch-require-run',
  },
]

export function PublishSettings({
  value,
  onChange,
}: {
  value: PublishSettingsValue
  onChange: (patch: Partial<PublishSettingsValue>) => void
}) {
  return (
    <section data-testid="publish-settings">
      <div className="text-[11px] uppercase tracking-[0.16em] text-ink-500 mb-3">
        Publish / Promote settings
      </div>
      <div className="space-y-1.5">
        {ROWS.map((row) => (
          <div
            key={row.key}
            className="flex items-center gap-3 rounded-lg px-3 py-2.5 border border-transparent hover:bg-white/[.02]"
          >
            <div className="flex-1 min-w-0">
              <div className="text-sm font-medium text-ink-200">{row.label}</div>
              <p className="text-[11px] text-ink-500 mt-0.5 leading-relaxed">{row.hint}</p>
            </div>
            <Switch
              on={value[row.key]}
              onToggle={() => onChange({ [row.key]: !value[row.key] })}
              label={row.label}
              testId={row.testId}
            />
          </div>
        ))}
      </div>
      <p className="text-[11px] text-ink-600 mt-3 px-3">
        Static validation and tool preflight always run and cannot be switched off.
      </p>
    </section>
  )
}
