/**
 * plans/010: tiny tokenizers behind the explain renderers.
 *
 * Pure functions (no React) so they test in the node environment. Every
 * token's text comes verbatim from the input — the renderers never invent
 * content, they only color what the definition already says.
 */

export interface Tok {
  text: string
  cls: TokCls
}

export type TokCls =
  | 'plain'      // ordinary identifier / text
  | 'ref-inputs' // inputs.* template reference
  | 'ref-steps'  // steps.* template reference
  | 'ref-vars'   // vars.* template reference
  | 'string'
  | 'number'
  | 'keyword'
  | 'op'         // operators / punctuation
  | 'delim'      // {{ }} jinja delimiters
  | 'comment'
  | 'decorator'
  | 'defname'    // name right after def/class

// Jinja + python expression keywords that appear inside `when`/`over`/etc.
const EXPR_KEYWORDS = new Set([
  'if', 'else', 'elif', 'and', 'or', 'not', 'in', 'is', 'for',
  'none', 'None', 'true', 'True', 'false', 'False',
])

const EXPR_RE =
  /("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')|(\d+(?:\.\d+)?)|([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)|(\{\{|\}\})|(\s+)|(.)/g

function classifyPath(path: string): TokCls {
  const root = path.split('.')[0]!
  if (root === 'inputs') return 'ref-inputs'
  if (root === 'steps') return 'ref-steps'
  if (root === 'vars') return 'ref-vars'
  if (EXPR_KEYWORDS.has(path)) return 'keyword'
  return 'plain'
}

/** Tokenize one expression (a `when`, `over`, `if`, … — with or without {{ }}). */
export function tokenizeExpr(src: string): Tok[] {
  const out: Tok[] = []
  for (const m of src.matchAll(EXPR_RE)) {
    const [, str, num, path, delim, ws, other] = m
    if (str !== undefined) out.push({ text: str, cls: 'string' })
    else if (num !== undefined) out.push({ text: num, cls: 'number' })
    else if (path !== undefined) out.push({ text: path, cls: classifyPath(path) })
    else if (delim !== undefined) out.push({ text: delim, cls: 'delim' })
    else if (ws !== undefined) out.push({ text: ws, cls: 'plain' })
    else out.push({ text: other!, cls: 'op' })
  }
  return out
}

/**
 * Split prose that may embed {{ ... }} spans into alternating segments.
 * Used for prompts: prose stays prose, template spans get expression colors.
 */
export function splitTemplate(text: string): { kind: 'text' | 'expr'; value: string }[] {
  const parts: { kind: 'text' | 'expr'; value: string }[] = []
  const re = /\{\{[\s\S]*?\}\}|\{%[\s\S]*?%\}/g
  let last = 0
  for (const m of text.matchAll(re)) {
    if (m.index! > last) parts.push({ kind: 'text', value: text.slice(last, m.index) })
    parts.push({ kind: 'expr', value: m[0] })
    last = m.index! + m[0].length
  }
  if (last < text.length) parts.push({ kind: 'text', value: text.slice(last) })
  return parts
}

const PY_KEYWORDS = new Set([
  'def', 'class', 'return', 'if', 'elif', 'else', 'for', 'while', 'in',
  'not', 'and', 'or', 'is', 'None', 'True', 'False', 'import', 'from',
  'as', 'try', 'except', 'finally', 'raise', 'with', 'lambda', 'yield',
  'pass', 'break', 'continue', 'global', 'nonlocal', 'assert', 'async',
  'await', 'del', 'match', 'case',
])

const PY_RE =
  /("""[\s\S]*?"""|'''[\s\S]*?''')|(#[^\n]*)|("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')|(@[A-Za-z_]\w*)|(\b\d+(?:\.\d+)?\b)|([A-Za-z_]\w*)|(\n)|([ \t]+)|(.)/g

/** Tokenize python source into lines of tokens (for line-numbered rendering). */
export function tokenizePython(src: string): Tok[][] {
  const lines: Tok[][] = [[]]
  let prevWord: string | null = null
  const push = (text: string, cls: Tok['cls']) => {
    // multi-line tokens (triple strings) get split across line arrays
    const segs = text.split('\n')
    segs.forEach((seg, i) => {
      if (i > 0) lines.push([])
      if (seg) lines[lines.length - 1]!.push({ text: seg, cls })
    })
  }
  for (const m of src.matchAll(PY_RE)) {
    const [, tri, comment, str, deco, num, word, nl, ws, other] = m
    if (tri !== undefined) { push(tri, 'string'); prevWord = null }
    else if (comment !== undefined) { push(comment, 'comment'); prevWord = null }
    else if (str !== undefined) { push(str, 'string'); prevWord = null }
    else if (deco !== undefined) { push(deco, 'decorator'); prevWord = null }
    else if (num !== undefined) { push(num, 'number'); prevWord = null }
    else if (word !== undefined) {
      if (PY_KEYWORDS.has(word)) push(word, 'keyword')
      else if (prevWord === 'def' || prevWord === 'class') push(word, 'defname')
      else push(word, 'plain')
      prevWord = word
    } else if (nl !== undefined) { lines.push([]) }
    else if (ws !== undefined) { push(ws, 'plain') }
    else { push(other!, 'op'); prevWord = null }
  }
  return lines
}

/**
 * Template references used by an expression — feeds the data-flow section.
 * Returns dotted paths trimmed to a readable depth (steps.x.y, inputs.x, vars.x).
 */
export function extractRefs(src: string): string[] {
  const refs: string[] = []
  for (const t of tokenizeExpr(src)) {
    if (t.cls === 'ref-inputs' || t.cls === 'ref-steps' || t.cls === 'ref-vars') {
      const depth = t.cls === 'ref-steps' ? 3 : 2
      refs.push(t.text.split('.').slice(0, depth).join('.'))
    }
  }
  return [...new Set(refs)]
}
