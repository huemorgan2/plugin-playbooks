/**
 * plans/010 phase 3: collapsible typed tree for run raw inputs/outputs.
 *
 * Zero-data rule: renders exactly the value it is given — no sample values,
 * no placeholder text. Scalars use the same color families as the definition
 * token palette (string amber, number amber-200, boolean fuchsia, null muted).
 */

import { useState } from 'react'
import { ChevronDown, ChevronRight } from 'lucide-react'

function Scalar({ value }: { value: unknown }) {
  if (value === null || value === undefined)
    return <span className="text-ink-500 italic">null</span>
  if (typeof value === 'string')
    return <span className="text-amber-300 break-all">"{value}"</span>
  if (typeof value === 'number')
    return <span className="text-amber-200">{String(value)}</span>
  if (typeof value === 'boolean')
    return <span className="text-fuchsia-300">{String(value)}</span>
  return <span className="text-ink-300">{String(value)}</span>
}

function isComposite(v: unknown): v is Record<string, unknown> | unknown[] {
  return v !== null && typeof v === 'object'
}

function summary(v: Record<string, unknown> | unknown[]): string {
  if (Array.isArray(v)) return v.length === 1 ? '1 item' : `${v.length} items`
  const n = Object.keys(v).length
  return n === 1 ? '1 key' : `${n} keys`
}

function Node({ name, value, depth }: { name: string | null; value: unknown; depth: number }) {
  // Deep nodes start collapsed so large payloads stay scannable.
  const [open, setOpen] = useState(depth < 2)

  const key = name != null && (
    <span className="font-mono font-semibold text-ink-200 shrink-0">{name}</span>
  )

  if (!isComposite(value)) {
    return (
      <div className="flex gap-1.5 items-baseline min-w-0" data-json-key={name ?? undefined}>
        {key}
        {name != null && <span className="text-ink-600 shrink-0">:</span>}
        <span className="font-mono min-w-0"><Scalar value={value} /></span>
      </div>
    )
  }

  const entries: [string, unknown][] = Array.isArray(value)
    ? value.map((v, i) => [String(i), v] as [string, unknown])
    : Object.entries(value)
  const bracket = Array.isArray(value) ? ['[', ']'] : ['{', '}']

  return (
    <div className="min-w-0" data-json-key={name ?? undefined}>
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex gap-1 items-center text-left min-w-0 hover:bg-white/5 rounded px-0.5 -mx-0.5 transition"
      >
        {open
          ? <ChevronDown className="w-2.5 h-2.5 text-ink-500 shrink-0" />
          : <ChevronRight className="w-2.5 h-2.5 text-ink-500 shrink-0" />}
        {key}
        {name != null && <span className="text-ink-600">:</span>}
        <span className="text-ink-600 font-mono">
          {bracket[0]}
          {!open && <span className="text-ink-500 not-italic mx-1 text-[9px]">{summary(value)}</span>}
          {!open && bracket[1]}
        </span>
      </button>
      {open && (
        <>
          <div className="pl-3.5 border-l border-white/5 ml-1 space-y-0.5">
            {entries.map(([k, v]) => (
              <Node key={k} name={k} value={v} depth={depth + 1} />
            ))}
            {entries.length === 0 && <span className="text-ink-600 text-[10px] italic">empty</span>}
          </div>
          <span className="text-ink-600 font-mono ml-4">{bracket[1]}</span>
        </>
      )}
    </div>
  )
}

/** Collapsible typed tree for a run payload (resolved inputs / raw output). */
export function JsonTree({ data }: { data: unknown }) {
  return (
    <div
      className="text-[10px] leading-relaxed bg-ink-900/60 rounded p-2 max-h-56 overflow-auto"
      data-testid="json-tree"
    >
      <Node name={null} value={data} depth={0} />
    </div>
  )
}
