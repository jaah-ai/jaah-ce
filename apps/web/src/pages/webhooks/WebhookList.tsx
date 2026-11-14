import type { JSX } from 'react'
import { absoluteTime, compactAge } from '../../lib/relativeTime'
import { hostOf } from '../../lib/hostOf'
import RecordFeed from '../../ui/feed/RecordFeed'
import type { FeedColumns, FeedRowSpec } from '../../ui/feed/types'
import { PHONE } from '../../ui/listStyles'
import { grid } from '../../theme'
import { deliveryState, listWebhooks, NEW_WEBHOOK_PATH, webhookPath, WEBHOOKS_KEY, type Webhook } from './api'

/* OUTGOING WEBHOOKS — the org's webhook endpoints, one feed row each (`GET /api/webhooks`, `@org_admin`; the routes
 * are guarded the same in `App.tsx`, and by the `webhooks_enabled` plan switch). A row: the endpoint's name, its
 * URL's host, how many event types it sends, and its latest delivery; it opens the endpoint's tabbed page
 * (`WebhookDetailPage.tsx`). New opens the same page in create mode. `WebhookFeed` is also Settings › Integrations ›
 * Webhooks' body (`pages/Settings.tsx`). */

const COLUMNS: FeedColumns = {
  title: { key: 'name', label: 'Name' },
  slots: [
    { key: 'host', label: 'URL' },
    { key: 'events', label: 'Events' },
    { key: 'last', label: 'Last delivery' },
  ],
}

function rowOf(hook: Webhook): FeedRowSpec {
  const last = hook.last_delivery
  const lastState = last ? deliveryState(last) : null
  return {
    key: hook.id,
    indicator: hook.enabled
      ? { label: 'Enabled', tone: 'success', glyph: '●' }
      : { label: 'Disabled', tone: 'neutral', glyph: '○' },
    title: hook.name,
    titleText: hook.name,
    titleWrapAt: PHONE,
    href: webhookPath(hook.id),
    muted: !hook.enabled,
    data: { enabled: String(hook.enabled) },
    slots: [
      { key: 'host', width: grid(30), gate: 'meta', align: 'left', dim: true, title: hook.url, node: hostOf(hook.url), testId: 'webhook-host' },
      { key: 'events', width: grid(9), align: 'right', mono: true, title: hook.event_types.join(', '), node: String(hook.event_types.length), testId: 'webhook-events' },
      {
        key: 'last', width: grid(16), align: 'right', dim: !last,
        title: last ? `${lastState?.label}${last.status_code !== null ? ` · ${last.status_code}` : ''} · ${absoluteTime(last.at)}` : 'Nothing sent yet',
        node: last ? `${lastState?.label} · ${compactAge(last.at)}` : '—',
        data: { state: last?.state ?? 'none' },
        testId: 'webhook-last',
      },
    ],
  }
}

const FEED = {
  id: 'webhooks', label: 'Webhooks', queryKey: WEBHOOKS_KEY, list: listWebhooks, rowOf, columns: COLUMNS,
  emptyTitle: 'No webhooks yet', newLabel: 'New webhook', newTo: NEW_WEBHOOK_PATH,
}

/** The list's body by read state: loading, failed, none yet, or the rows. */
export const WebhookFeed = (): JSX.Element => <RecordFeed {...FEED} />

export default function WebhookList(): JSX.Element {
  return <RecordFeed {...FEED} page />
}
