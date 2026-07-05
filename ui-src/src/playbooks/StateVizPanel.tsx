/**
 * 007.009.01 — live stack / queue / set / counter visualization.
 *
 * Renders each run-scoped var as a card of chips. As the replay scrubs, a
 * push slides the new element IN (and pulses), a pop slides the leaving element
 * OUT at the right end (front for a queue, top for a stack). The shape label is
 * inferred from how the var is used across the run.
 */

import { cn } from '../lib/cn'
import type { StateFrame } from './types'
import { type TimelineFrame, type VarKind, snapshotAt, classifyVars, isPush } from './runReplay'

const KIND_META: Record<VarKind, { label: string; hint: string; rgb: string }> = {
  stack:   { label: 'stack',   hint: 'LIFO · pop top',    rgb: '192 132 252' },
  queue:   { label: 'queue',   hint: 'FIFO · pop front',  rgb: '52 211 153' },
  set:     { label: 'set',     hint: 'unique',            rgb: '56 189 248' },
  counter: { label: 'counter', hint: '',                  rgb: '251 191 36' },
  list:    { label: 'list',    hint: '',                  rgb: '129 140 248' },
  value:   { label: 'value',   hint: '',                  rgb: '148 163 184' },
}

function fmt(v: any): string {
  if (v == null) return '∅'
  if (typeof v === 'string') return v
  if (typeof v === 'object') return JSON.stringify(v)
  return String(v)
}

export function StateVizPanel({
  timeline,
  idx,
}: {
  timeline: TimelineFrame[]
  idx: number
}) {
  const snap = snapshotAt(timeline, idx)
  const kinds = classifyVars(timeline)
  if (snap.order.length === 0) return null

  // op applied to each var at THIS frame (for enter/leave animation)
  const opByVar = new Map<string, StateFrame>()
  for (const f of snap.current) opByVar.set(f.var, f)

  return (
    <div
      data-testid="state-viz-panel"
      className="absolute bottom-3 left-3 right-3 z-10 flex gap-3 overflow-x-auto pb-1"
    >
      {snap.order.map((name) => {
        const value = snap.vars[name]
        const kind = kinds[name] || 'list'
        const meta = KIND_META[kind]
        const op = opByVar.get(name)
        return (
          <div
            key={name}
            data-testid={`viz-var-${name}`}
            style={{ ['--glow-rgb' as string]: meta.rgb }}
            className="shrink-0 min-w-[160px] max-w-[420px] rounded-xl border border-white/10 bg-ink-950/85 backdrop-blur-sm px-3 py-2 shadow-lg"
          >
            <div className="flex items-center gap-2 mb-1.5">
              <span className="text-xs font-mono font-semibold text-ink-100">{name}</span>
              <span
                className="text-[9px] uppercase tracking-wider px-1.5 py-0.5 rounded"
                style={{ color: `rgb(${meta.rgb})`, background: `rgba(${meta.rgb}, 0.12)` }}
              >
                {meta.label}
              </span>
              {meta.hint && <span className="text-[9px] text-ink-600">{meta.hint}</span>}
            </div>
            <VarBody name={name} value={value} kind={kind} op={op} frameKey={idx} />
          </div>
        )
      })}
    </div>
  )
}

function VarBody({
  value, kind, op, frameKey,
}: {
  name: string
  value: any
  kind: VarKind
  op?: StateFrame
  frameKey: number
}) {
  if (kind === 'counter' || kind === 'value' || (!Array.isArray(value) && typeof value !== 'object')) {
    return (
      <div
        key={`${frameKey}-${fmt(value)}`}
        className={cn('text-2xl font-mono font-bold text-ink-100', op && 'viz-item-pulse rounded px-1')}
      >
        {fmt(value)}
      </div>
    )
  }

  if (!Array.isArray(value)) {
    // dict / object
    return (
      <pre className="text-[10px] font-mono text-ink-300 max-h-24 overflow-auto whitespace-pre-wrap">
        {JSON.stringify(value, null, 2)}
      </pre>
    )
  }

  const list: any[] = value
  const popFront = op && op.op === 'pop_front'
  const popBack = op && op.op === 'pop_back'
  // which element just arrived?
  let enterIndex = -1
  if (op && isPush(op.op)) {
    enterIndex = op.op === 'push_front' ? 0 : list.length - 1
  }

  return (
    <div className="flex items-center gap-1.5 flex-wrap">
      {/* ghost of the element that just left, at the front */}
      {popFront && op && (
        <span
          key={`leave-front-${frameKey}`}
          className="viz-item-leave inline-flex items-center gap-1 px-2 py-1 rounded-md text-[11px] font-mono bg-rose-900/40 text-rose-200 border border-dashed border-rose-400/60"
        >
          {fmt(op.item)}<span className="text-[8px] uppercase tracking-wide text-rose-300/80">out ↤</span>
        </span>
      )}
      {list.length === 0 && !popFront && !popBack && (
        <span className="text-[11px] text-ink-600 italic">empty</span>
      )}
      {list.map((el, i) => (
        <span
          key={i === enterIndex ? `enter-${frameKey}-${i}` : `el-${i}-${fmt(el)}`}
          data-testid="viz-chip"
          className={cn(
            'px-2 py-1 rounded-md text-[11px] font-mono border',
            i === enterIndex
              ? 'viz-item-enter viz-item-pulse bg-emerald-900/40 text-emerald-100 border-emerald-400/40'
              : 'bg-ink-800/70 text-ink-200 border-white/10',
          )}
        >
          {fmt(el)}
        </span>
      ))}
      {/* ghost of the element that just left, at the back (stack) */}
      {popBack && op && (
        <span
          key={`leave-back-${frameKey}`}
          className="viz-item-leave inline-flex items-center gap-1 px-2 py-1 rounded-md text-[11px] font-mono bg-rose-900/40 text-rose-200 border border-dashed border-rose-400/60"
        >
          <span className="text-[8px] uppercase tracking-wide text-rose-300/80">out ↥</span>{fmt(op.item)}
        </span>
      )}
    </div>
  )
}
