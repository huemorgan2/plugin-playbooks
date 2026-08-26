/**
 * ManifestTab (0.13.0, plans/002 phase 6) — the playbook's intent page.
 * Plain free text the owner writes; the agent must keep the playbook true
 * to it (promote checks manifest drift). Saving bumps the live version.
 */
import { useEffect, useState } from 'react'
import { Loader2, Check } from 'lucide-react'
import { playbooksApi } from './api'

export function ManifestTab({
  name,
  onSaved,
}: {
  name: string
  onSaved: (version: number) => void
}) {
  const [text, setText] = useState<string | null>(null)
  const [origText, setOrigText] = useState('')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    playbooksApi.getManifest(name)
      .then((r) => {
        setText(r.manifest ?? '')
        setOrigText(r.manifest ?? '')
      })
      .catch((e) => setError(e.message))
  }, [name])

  const save = async () => {
    if (saving || text === null) return
    setSaving(true)
    setError(null)
    try {
      const r = await playbooksApi.putManifest(name, text)
      setOrigText(text)
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
      onSaved(r.version)
    } catch (e: any) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  if (text === null) {
    return (
      <div className="flex items-center justify-center h-full text-ink-400">
        {error
          ? <p className="text-xs text-rose-400">{error}</p>
          : <Loader2 className="w-5 h-5 animate-spin" />}
      </div>
    )
  }

  const dirty = text !== origText

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-2xl mx-auto py-4 px-4 flex flex-col h-full">
        <div className="flex items-center justify-between">
          <div className="text-[11px] uppercase tracking-[0.16em] text-ink-500">Intent</div>
          <button
            onClick={save}
            disabled={saving || !dirty}
            className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-luna-600 hover:bg-luna-500 disabled:opacity-40 text-white text-xs font-medium transition"
            data-testid="manifest-save"
          >
            {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" />
              : saved ? <Check className="w-3.5 h-3.5" /> : null}
            {saved ? 'Saved' : 'Save'}
          </button>
        </div>
        <p className="text-xs text-ink-500 mt-1 mb-3">
          What this playbook is for, in your words. Changes to the playbook are
          checked against it before going live.
        </p>
        {error && <p className="text-xs text-rose-400 mb-2">{error}</p>}
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={'No intent written yet.\n\nSay what this playbook should do, what it must never do, and what "done" looks like.'}
          className="flex-1 min-h-[320px] w-full resize-none rounded-xl border border-white/10 bg-ink-900/40 p-4 text-sm text-ink-200 leading-relaxed placeholder:text-ink-600 focus:outline-none focus:border-luna-500/50"
          data-testid="manifest-textarea"
          spellCheck={false}
        />
      </div>
    </div>
  )
}
