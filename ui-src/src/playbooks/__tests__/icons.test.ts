import { describe, expect, it } from 'vitest'
import { buildIconRef, toolIconUrl, triggerIconUrl } from '../icons'
import type { IconReference, PluginListEntry, ConnectorsStatus } from '../api'

const reference = (over: Partial<IconReference> = {}): IconReference => ({
  tools: {},
  triggers: [],
  ...over,
})

const plugins: PluginListEntry[] = [
  { name: 'plugin-monday', has_image: true },
  { name: 'plugin-connectors', has_image: true },
  { name: 'plugin-stackone', has_image: false },
]

describe('buildIconRef', () => {
  it('maps a tool to its owning plugin icon route', () => {
    const ref = buildIconRef(
      reference({ tools: { monday_boards_list: 'plugin-monday' } }),
      plugins,
      null,
    )
    expect(toolIconUrl(ref, 'monday_boards_list')).toBe('/api/plugins/plugin-monday/icon')
  })

  it('keeps the glyph for a plugin without a real image (no default png)', () => {
    const ref = buildIconRef(
      reference({ tools: { stackone_list_employees: 'plugin-stackone' } }),
      plugins,
      null,
    )
    expect(toolIconUrl(ref, 'stackone_list_employees')).toBeNull()
  })

  it('keeps the glyph for an unknown tool or missing reference', () => {
    const ref = buildIconRef(reference(), plugins, null)
    expect(toolIconUrl(ref, 'nope')).toBeNull()
    expect(toolIconUrl(null, 'monday_boards_list')).toBeNull()
    expect(toolIconUrl(ref, undefined)).toBeNull()
  })

  it('prefers the real integration logo for connector triggers', () => {
    const connectors: ConnectorsStatus = {
      apps: [{ slug: 'gmail', logo: 'https://cdn.example/gmail.png' }, { slug: 'slack', logo: null }],
    }
    const ref = buildIconRef(
      reference({
        triggers: [
          {
            event_pattern: 'connector.gmail.new_gmail_message',
            source: 'connectors', app: 'gmail', label: 'New email', plugin: 'plugin-connectors',
          },
          {
            event_pattern: 'connector.slack.new_message',
            source: 'connectors', app: 'slack', label: 'New message', plugin: 'plugin-connectors',
          },
        ],
      }),
      plugins,
      connectors,
    )
    expect(triggerIconUrl(ref, 'connector.gmail.new_gmail_message'))
      .toBe('https://cdn.example/gmail.png')
    // no CDN logo → publisher plugin icon
    expect(triggerIconUrl(ref, 'connector.slack.new_message'))
      .toBe('/api/plugins/plugin-connectors/icon')
  })

  it('falls back to the publisher plugin icon for non-connector triggers', () => {
    const ref = buildIconRef(
      reference({
        triggers: [{
          event_pattern: 'monday.item_created',
          source: 'monday', app: 'monday', label: 'Item created', plugin: 'plugin-monday',
        }],
      }),
      plugins,
      null,
    )
    expect(triggerIconUrl(ref, 'monday.item_created')).toBe('/api/plugins/plugin-monday/icon')
  })

  it('drops triggers with no resolvable image at all', () => {
    const ref = buildIconRef(
      reference({
        triggers: [{
          event_pattern: 'stackone.bamboohr.employee_created',
          source: 'stackone', app: 'stackone', label: 'x', plugin: 'plugin-stackone',
        }],
      }),
      plugins,
      null,
    )
    expect(triggerIconUrl(ref, 'stackone.bamboohr.employee_created')).toBeNull()
  })
})

describe('hosted base-path prefix', () => {
  it('prefixes plugin icon URLs with window.__LUNA_BASE (img src bypasses the fetch shim)', () => {
    ;(globalThis as any).window = { __LUNA_BASE: '/a/vaselin-scanny-2' }
    try {
      const ref = buildIconRef(
        reference({ tools: { monday_boards_list: 'plugin-monday' } }),
        plugins,
        null,
      )
      expect(toolIconUrl(ref, 'monday_boards_list'))
        .toBe('/a/vaselin-scanny-2/api/plugins/plugin-monday/icon')
    } finally {
      delete (globalThis as any).window
    }
  })
})

describe('triggerIconUrl pattern matching', () => {
  const ref = buildIconRef(
    reference({
      triggers: [{
        event_pattern: 'connector.gmail.*',
        source: 'connectors', app: 'gmail', label: 'Gmail', plugin: 'plugin-connectors',
      }],
    }),
    plugins,
    { apps: [{ slug: 'gmail', logo: 'https://cdn.example/gmail.png' }] },
  )

  it('matches wildcard patterns', () => {
    expect(triggerIconUrl(ref, 'connector.gmail.new_gmail_message'))
      .toBe('https://cdn.example/gmail.png')
  })

  it('does not treat dots as regex wildcards', () => {
    expect(triggerIconUrl(ref, 'connectorXgmailXanything')).toBeNull()
  })

  it('returns null for cron/absent events', () => {
    expect(triggerIconUrl(ref, null)).toBeNull()
    expect(triggerIconUrl(ref, 'other.event')).toBeNull()
  })
})
