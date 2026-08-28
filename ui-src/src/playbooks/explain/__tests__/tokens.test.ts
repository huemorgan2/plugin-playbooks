import { describe, it, expect } from 'vitest'
import { tokenizeExpr, tokenizePython, splitTemplate, extractRefs } from '../tokens'

describe('tokenizeExpr', () => {
  it('reassembles to the exact input (no invented or lost text)', () => {
    const src = "{{ steps.fetch.result | length > 3 and inputs.name == 'x' }}"
    expect(tokenizeExpr(src).map((t) => t.text).join('')).toBe(src)
  })

  it('classifies template roots semantically', () => {
    const toks = tokenizeExpr('inputs.email and steps.fetch.result or vars.queue')
    const byText = Object.fromEntries(toks.map((t) => [t.text, t.cls]))
    expect(byText['inputs.email']).toBe('ref-inputs')
    expect(byText['steps.fetch.result']).toBe('ref-steps')
    expect(byText['vars.queue']).toBe('ref-vars')
    expect(byText['and']).toBe('keyword')
  })

  it('classifies literals', () => {
    const toks = tokenizeExpr("count > 42 and mode == 'fast'")
    expect(toks.find((t) => t.text === '42')?.cls).toBe('number')
    expect(toks.find((t) => t.text === "'fast'")?.cls).toBe('string')
  })
})

describe('splitTemplate', () => {
  it('separates prose from jinja spans without losing characters', () => {
    const src = 'Summarize {{ steps.fetch.result }} for {{ inputs.name }}.'
    const parts = splitTemplate(src)
    expect(parts.map((p) => p.value).join('')).toBe(src)
    expect(parts.filter((p) => p.kind === 'expr')).toHaveLength(2)
  })

  it('returns one text part when there is no template', () => {
    expect(splitTemplate('plain prose')).toEqual([{ kind: 'text', value: 'plain prose' }])
  })
})

describe('tokenizePython', () => {
  it('reassembles line-exact source', () => {
    const src = 'def f(x):\n    # double\n    return x * 2\n'
    const lines = tokenizePython(src)
    const rebuilt = lines.map((l) => l.map((t) => t.text).join('')).join('\n')
    expect(rebuilt).toBe(src + '')
  })

  it('classifies keywords, names, comments, strings', () => {
    const lines = tokenizePython("def add(a):\n    return a + 'x'  # note")
    const flat = lines.flat()
    expect(flat.find((t) => t.text === 'def')?.cls).toBe('keyword')
    expect(flat.find((t) => t.text === 'add')?.cls).toBe('defname')
    expect(flat.find((t) => t.text === "'x'")?.cls).toBe('string')
    expect(flat.find((t) => t.text === '# note')?.cls).toBe('comment')
  })

  it('keeps triple-quoted strings whole across lines', () => {
    const lines = tokenizePython('s = """a\nb"""\nx = 1')
    expect(lines).toHaveLength(3)
    expect(lines[0]!.some((t) => t.cls === 'string')).toBe(true)
    expect(lines[1]!.some((t) => t.cls === 'string')).toBe(true)
  })
})

describe('extractRefs', () => {
  it('collects unique template references at readable depth', () => {
    const refs = extractRefs('{{ steps.fetch.result.items and inputs.email or vars.seen and steps.fetch.result }}')
    expect(refs).toEqual(['steps.fetch.result', 'inputs.email', 'vars.seen'])
  })

  it('returns nothing for pure literals', () => {
    expect(extractRefs("'hello' + 42")).toEqual([])
  })
})
