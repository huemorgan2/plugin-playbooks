/**
 * plans/011: integration icons. A tool step shows the app icon of the plugin
 * that owns the tool; a trigger shows the real integration logo (Composio
 * CDN for connector triggers, the publisher plugin's icon otherwise). Every
 * resolution is best-effort — anything unknown falls back to the kind glyph,
 * and a plugin without a real image keeps the glyph rather than showing the
 * generic default.
 */

import { useEffect, useReducer, useState, type ComponentType } from 'react'
import { cn } from '../lib/cn'
import { iconsApi, type IconReference, type PluginListEntry, type ConnectorsStatus } from './api'

export interface IconRef {
  /** tool name -> image URL */
  toolIcons: Record<string, string>
  /** trigger event pattern -> image URL, in registry order */
  triggerIcons: { pattern: string; url: string }[]
}

/** Pure join of the three sources; exported for tests. */
export function buildIconRef(
  reference: IconReference,
  plugins: PluginListEntry[],
  connectors: ConnectorsStatus | null,
): IconRef {
  const hasImage = new Set(plugins.filter((p) => p.has_image).map((p) => p.name))
  const pluginIcon = (plugin: string | null | undefined): string | null =>
    plugin && hasImage.has(plugin) ? `/api/plugins/${encodeURIComponent(plugin)}/icon` : null

  const toolIcons: Record<string, string> = {}
  for (const [tool, plugin] of Object.entries(reference.tools || {})) {
    const url = pluginIcon(plugin)
    if (url) toolIcons[tool] = url
  }

  const appLogos: Record<string, string> = {}
  for (const a of connectors?.apps || []) {
    if (a.logo) appLogos[a.slug] = a.logo
  }

  const triggerIcons: { pattern: string; url: string }[] = []
  for (const t of reference.triggers || []) {
    const url = (t.source === 'connectors' && appLogos[t.app]) || pluginIcon(t.plugin)
    if (url) triggerIcons.push({ pattern: t.event_pattern, url })
  }

  return { toolIcons, triggerIcons }
}

export function toolIconUrl(ref: IconRef | null, tool?: string | null): string | null {
  if (!ref || !tool) return null
  return ref.toolIcons[tool] || null
}

function patternMatches(pattern: string, event: string): boolean {
  if (pattern === event) return true
  if (!pattern.includes('*')) return false
  const re = new RegExp(
    '^' + pattern.split('*').map((s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('.*') + '$',
  )
  return re.test(event)
}

export function triggerIconUrl(ref: IconRef | null, event?: string | null): string | null {
  if (!ref || !event) return null
  for (const t of ref.triggerIcons) {
    if (patternMatches(t.pattern, event)) return t.url
  }
  return null
}

// ---- session-cached loader + hook ----

let _ref: IconRef | null = null
let _started = false
const _subs = new Set<() => void>()

async function _load(): Promise<void> {
  // The reference is required; the two joins are best-effort (connectors may
  // not be installed, /api/plugins may be denied) and default to empty.
  const [reference, plugins, connectors] = await Promise.all([
    iconsApi.reference(),
    iconsApi.plugins().catch(() => [] as PluginListEntry[]),
    iconsApi.connectorsStatus().catch(() => null),
  ])
  _ref = buildIconRef(reference, plugins, connectors)
  _subs.forEach((f) => f())
}

/** The shared icon reference; null until loaded (render the kind glyph). */
export function useIconRef(): IconRef | null {
  const [, force] = useReducer((x: number) => x + 1, 0)
  useEffect(() => {
    if (!_started) {
      _started = true
      _load().catch(() => {})
    }
    const f = () => force()
    _subs.add(f)
    return () => {
      _subs.delete(f)
    }
  }, [])
  return _ref
}

/** An integration image with a hard fallback to the kind glyph. */
export function IntegrationIcon({
  url, fallback: Fallback, fallbackClass, className,
}: {
  url: string | null
  fallback: ComponentType<{ className?: string }>
  fallbackClass?: string
  className?: string
}) {
  const [failed, setFailed] = useState(false)
  useEffect(() => setFailed(false), [url])
  if (!url || failed) return <Fallback className={fallbackClass} />
  return (
    <img
      src={url}
      alt=""
      draggable={false}
      onError={() => setFailed(true)}
      className={cn('rounded-[4px] object-contain shrink-0', className)}
      data-testid="integration-icon"
    />
  )
}
