/**
 * ToolsGuide — the ⓘ panel on the Playbooks section: a short primer plus a
 * table of every playbook tool the agent can use, so owners can see exactly
 * what Luna is able to do with playbooks on their behalf.
 */

const TOOLS: { name: string; what: string }[] = [
  { name: 'playbook_list', what: 'List your playbooks — names, descriptions, and who is allowed to run each one.' },
  { name: 'playbook_propose', what: 'Create a new playbook from a full YAML definition (steps, triggers, and inputs all at once).' },
  { name: 'playbook_get_definition', what: 'Read the complete YAML of an existing playbook before making changes.' },
  { name: 'playbook_edit', what: 'Rewrite a playbook in place. A version snapshot is saved first, so every change can be traced.' },
  { name: 'playbook_validate', what: 'Statically check a playbook without running it — schema errors, bad references, unknown tools, cycles.' },
  { name: 'playbook_dry_run', what: 'Simulate a run with no side effects: real loops and branches, but tool and LLM steps are stubbed.' },
  { name: 'playbook_run', what: 'Start a real run in the background. Quick runs return results immediately; long ones hand back a run id to follow.' },
  { name: 'playbook_status', what: 'Follow a run live: overall status, timing, and each step’s inputs, outputs, and errors.' },
  { name: 'playbook_cancel', what: 'Stop a running playbook mid-flight.' },
  { name: 'playbook_set_autonomy', what: 'Change who may trigger a playbook: agent runs it freely, must confirm with you first, or manual only. Always asks you before changing.' },
]

export function ToolsGuide() {
  return (
    <div className="mx-6 mt-3 rounded-xl border border-white/10 bg-white/[.03] p-5 shrink-0 max-h-[50vh] overflow-y-auto" data-testid="tools-guide">
      <h3 className="text-sm font-semibold text-ink-100">What playbooks can do</h3>
      <p className="text-xs text-ink-400 mt-1.5 leading-relaxed max-w-3xl">
        A playbook is a saved multi-step workflow — searches, tool calls, loops, decisions —
        that Luna authors as YAML and runs step by step, with every step&apos;s inputs and
        outputs recorded. You can ask Luna in chat to build, change, test, or run one, or edit
        it yourself in the canvas. These are the tools Luna uses under the hood:
      </p>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full text-left text-xs">
          <thead>
            <tr className="text-ink-500 border-b border-white/10">
              <th className="py-2 pr-4 font-medium whitespace-nowrap">Tool</th>
              <th className="py-2 font-medium">What it does</th>
            </tr>
          </thead>
          <tbody>
            {TOOLS.map((t) => (
              <tr key={t.name} className="border-b border-white/5 last:border-0 align-top">
                <td className="py-2 pr-4 whitespace-nowrap">
                  <code className="text-[11px] text-luna-400 bg-luna-600/10 rounded px-1.5 py-0.5">{t.name}</code>
                </td>
                <td className="py-2 text-ink-300 leading-relaxed">{t.what}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}
