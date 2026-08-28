/**
 * plans/010: shared building blocks for the per-kind explain renderers.
 *
 * Zero-data rule: every component takes definition content as props and
 * renders ONLY that content. No sample values, no fallback text standing in
 * for data — an absent field means the caller doesn't render the block.
 */

import {
  Bot, Wrench, GitBranch, Layers, Clock, Mail, RotateCcw,
  ExternalLink, Sparkles, Database, Ban, Code2, Zap,
} from 'lucide-react'
import { cn } from '../../lib/cn'
import { STEP_COLORS, type StepDef, type StepKind } from '../types'
import { IntegrationIcon, toolIconUrl, useIconRef } from '../icons'
import { tokenizeExpr, tokenizePython, splitTemplate, type Tok, type TokCls } from './tokens'

export const KIND_ICONS: Record<StepKind, React.ComponentType<{ className?: string }>> = {
  agent_step: Bot,
  llm_step: Sparkles,
  tool_call: Wrench,
  condition: GitBranch,
  parallel: Layers,
  wait_for_approval: Clock,
  wait_for_event: Mail,
  subtask: ExternalLink,
  loop: RotateCcw,
  state: Database,
  halt: Ban,
  code: Code2,
}

export function kindIcon(kind: StepKind): React.ComponentType<{ className?: string }> {
  return KIND_ICONS[kind] || Zap
}

/** Human spelling of a kind for eyebrows/labels — no internal snake_case on screen. */
export const KIND_LABELS: Record<StepKind, string> = {
  agent_step: 'Agent step',
  llm_step: 'LLM step',
  tool_call: 'Tool call',
  condition: 'Condition',
  parallel: 'Parallel',
  wait_for_approval: 'Wait for approval',
  wait_for_event: 'Wait for event',
  subtask: 'Sub-playbook',
  loop: 'Loop',
  state: 'State update',
  halt: 'Stop',
  code: 'Python code',
}

const TOK_CLS: Record<TokCls, string> = {
  'plain': 'text-ink-200',
  'ref-inputs': 'text-sky-300',
  'ref-steps': 'text-violet-300',
  'ref-vars': 'text-emerald-300',
  'string': 'text-amber-300',
  'number': 'text-amber-200',
  'keyword': 'text-fuchsia-300',
  'op': 'text-ink-500',
  'delim': 'text-ink-600',
  'comment': 'text-ink-500 italic',
  'decorator': 'text-violet-300',
  'defname': 'text-sky-300',
}

function TokSpans({ toks }: { toks: Tok[] }) {
  return (
    <>
      {toks.map((t, i) => (
        <span key={i} className={TOK_CLS[t.cls]}>{t.text}</span>
      ))}
    </>
  )
}

/** One expression (`when`, `over`, `if`, …) with semantic reference colors. */
export function Expr({ expr, className }: { expr: string; className?: string }) {
  return (
    <code className={cn('font-mono text-[11px] break-words', className)} data-expr={expr}>
      <TokSpans toks={tokenizeExpr(expr)} />
    </code>
  )
}

/** Prose (a prompt, a system message) with embedded {{ … }} spans colored. */
export function TemplateText({ text }: { text: string }) {
  const parts = splitTemplate(text)
  return (
    <p className="text-xs text-ink-300 leading-relaxed whitespace-pre-wrap break-words">
      {parts.map((p, i) =>
        p.kind === 'text' ? (
          <span key={i}>{p.value}</span>
        ) : (
          <code key={i} className="font-mono text-[11px] px-1 py-px rounded bg-ink-900/80 border border-white/5">
            <TokSpans toks={tokenizeExpr(p.value)} />
          </code>
        ),
      )}
    </p>
  )
}

/** A literal from the definition, colored by type; expression strings delegate to Expr. */
export function Value({ value, depth = 0 }: { value: unknown; depth?: number }) {
  if (value === null || value === undefined)
    return <span className="text-ink-500 italic">null</span>
  if (typeof value === 'string') {
    if (value.includes('{{') || value.includes('{%')) return <Expr expr={value} />
    return <span className="text-amber-300">"{value}"</span>
  }
  if (typeof value === 'number')
    return <span className="text-amber-200">{String(value)}</span>
  if (typeof value === 'boolean')
    return <span className="text-fuchsia-300">{String(value)}</span>
  if (Array.isArray(value)) {
    return (
      <span className="inline-flex flex-wrap items-baseline gap-x-1">
        <span className="text-ink-500">[</span>
        {value.map((v, i) => (
          <span key={i} className="inline-flex items-baseline">
            <Value value={v} depth={depth + 1} />
            {i < value.length - 1 && <span className="text-ink-500">,</span>}
          </span>
        ))}
        <span className="text-ink-500">]</span>
      </span>
    )
  }
  // object → nested rows
  return <KVTable data={value as Record<string, unknown>} nested />
}

/** Key/value grid for args, code_inputs, filters, maps. */
export function KVTable({
  data, nested = false, arrow = false,
}: {
  data: Record<string, unknown>
  nested?: boolean
  /** render `key → value` (for inputs_map / returns mappings) */
  arrow?: boolean
}) {
  const entries = Object.entries(data)
  if (!entries.length) return null
  return (
    <div className={cn('space-y-1 min-w-0', nested && 'pl-3 border-l border-white/5 mt-1')}>
      {entries.map(([k, v]) => (
        <div key={k} className="flex gap-2 items-baseline text-[11px] min-w-0" data-kv-key={k}>
          <span className="font-mono text-ink-400 shrink-0">{k}</span>
          {arrow && <span className="text-ink-600 shrink-0">←</span>}
          <div className="font-mono min-w-0 break-words">
            {typeof v === 'string' && arrow ? <Expr expr={v} /> : <Value value={v} />}
          </div>
        </div>
      ))}
    </div>
  )
}

/** JSON-Schema rendered as a field tree — names, type badges, required dots. */
export function SchemaTree({ schema, depth = 0 }: { schema: Record<string, any>; depth?: number }) {
  const props: Record<string, any> = schema?.properties || {}
  const required: string[] = Array.isArray(schema?.required) ? schema.required : []
  const names = Object.keys(props)
  if (!names.length) return null
  return (
    <div className={cn('space-y-1', depth > 0 && 'pl-3 border-l border-white/5 mt-1')}>
      {names.map((name) => {
        const f = props[name] || {}
        const type = f.type || (f.enum ? 'enum' : 'any')
        return (
          <div key={name} data-schema-field={name}>
            <div className="flex items-center gap-1.5 text-[11px]">
              {required.includes(name) && (
                <span className="w-1 h-1 rounded-full bg-amber-400 shrink-0" title="required" />
              )}
              <span className="font-mono text-ink-200">{name}</span>
              <span className="px-1 py-px rounded text-[9px] font-medium bg-ink-800 text-ink-400">
                {type === 'array' && f.items?.type ? `array<${f.items.type}>` : type}
              </span>
            </div>
            {f.description && (
              <p className="text-[10px] text-ink-500 ml-2.5 leading-snug">{f.description}</p>
            )}
            {f.enum && (
              <div className="flex flex-wrap gap-1 ml-2.5 mt-0.5">
                {f.enum.map((v: any) => (
                  <span key={String(v)} className="px-1 py-px rounded text-[9px] font-mono bg-ink-900 text-amber-300/80 border border-white/5">
                    {String(v)}
                  </span>
                ))}
              </div>
            )}
            {f.type === 'object' && f.properties && <SchemaTree schema={f} depth={depth + 1} />}
            {f.type === 'array' && f.items?.type === 'object' && f.items.properties && (
              <SchemaTree schema={f.items} depth={depth + 1} />
            )}
          </div>
        )
      })}
    </div>
  )
}

/** Read-only, line-numbered, highlighted python source. */
export function Code({ source }: { source: string }) {
  const lines = tokenizePython(source)
  return (
    <pre
      className="text-[11px] font-mono bg-ink-900/60 rounded-lg p-3 border border-white/5 overflow-x-auto leading-relaxed"
      data-testid="explain-code"
    >
      {lines.map((toks, i) => (
        <div key={i} className="flex min-w-0">
          <span className="w-7 shrink-0 select-none text-right pr-3 text-ink-600">{i + 1}</span>
          <span className="whitespace-pre">{toks.length ? <TokSpans toks={toks} /> : ' '}</span>
        </div>
      ))}
    </pre>
  )
}

/** One-line rows for nested steps; clicking selects that node on the canvas. */
export function StepList({
  steps, onSelect,
}: {
  steps: StepDef[]
  onSelect?: (step: StepDef) => void
}) {
  const iconRef = useIconRef()
  if (!steps.length) return null
  return (
    <div className="space-y-0.5">
      {steps.map((s) => {
        const colors = STEP_COLORS[s.kind] || STEP_COLORS.tool_call
        const Icon = kindIcon(s.kind)
        const toolUrl = s.kind === 'tool_call' ? toolIconUrl(iconRef, s.tool) : null
        return (
          <button
            key={s.id}
            onClick={() => onSelect?.(s)}
            className="w-full flex items-center gap-2 px-2 py-1 rounded-md text-left hover:bg-white/5 transition min-w-0"
            data-steplist-id={s.id}
          >
            <IntegrationIcon
              url={toolUrl}
              fallback={Icon}
              fallbackClass={cn('w-3 h-3 shrink-0', colors.text)}
              className="w-3 h-3"
            />
            <span className={cn('text-[11px] font-mono truncate', colors.text)}>{s.id}</span>
            <span className="text-[10px] text-ink-500 truncate ml-auto shrink-0">
              {KIND_LABELS[s.kind] || s.kind}
            </span>
          </button>
        )
      })}
    </div>
  )
}

/** Section label inside an explainer (structural, not data). */
export function SectionLabel({ children }: { children: React.ReactNode }) {
  return <div className="text-[10px] uppercase tracking-wider text-ink-600 mb-1">{children}</div>
}

/** Small colored pill for a single load-bearing name (tool, event, playbook). */
export function NamePill({ text, className }: { text: string; className?: string }) {
  return (
    <span className={cn(
      'inline-block px-2 py-0.5 rounded-full border text-[11px] font-mono',
      className || 'border-white/10 text-ink-200 bg-ink-900/60',
    )}>
      {text}
    </span>
  )
}
