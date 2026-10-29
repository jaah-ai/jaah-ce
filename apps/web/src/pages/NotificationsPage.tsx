import { useState } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import { apiDetail } from '../api'
import { TabPanel, Tabs, type TabDef } from '../components/Tabs'
import { canMarkRead, canMarkUnread, describeItem, isCommentKind, isUnread, itemTarget } from '../components/inbox/inboxItem'
import { hasMarkable, useInboxActions, useInboxFeed, type InboxView } from '../components/inbox/useInbox'
import { useUrlTab } from '../hooks/useUrlTab'
import { INBOX_LIMIT, INBOX_MAX, type InboxItem, type InboxKind } from '../lib/accessRequests'
import { absoluteTime, relativeAge } from '../lib/relativeTime'
import EmptyState from '../ui/EmptyState'
import FeedList from '../ui/feed/FeedList'
import type { FeedIndicator, FeedRowSpec, OverflowItem } from '../ui/feed/types'
import LoadFailedState from '../ui/LoadFailedState'
import LoadingState from '../ui/LoadingState'
import PageHeader from '../ui/PageHeader'
import { PHONE, tapTargetSx } from '../ui/listStyles'

/* Every notification, as a page: the bell's tray (`components/MentionsBell.tsx`, its View all) at full size. The same
 * two feeds behind the same switch, Unread | All, as tabs in the URL (`?tab=all`; Unread is the bare path), and the
 * same writes (`components/inbox/useInbox.ts`). A row opens what it is about and marks it read; its disc marks it
 * read, or (read) unread again. Load more widens the page to the API's cap (`INBOX_MAX`): the feed is derived from
 * each kind's own rows (`apps/api/app/routes/inbox.py`), so there is no cursor to page by. */

const TABS: readonly TabDef<InboxView>[] = [
  { id: 'unread', label: 'Unread' },
  { id: 'all', label: 'All' },
]

const KIND_LABEL: Record<InboxKind, string> = {
  mention: 'Mention',
  reaction: 'Reaction',
  access_request: 'Access',
  access_decision: 'Access',
  support_ticket: 'Support',
  support_task_done: 'Support',
  billing: 'Billing',
  trigger_failed: 'Trigger',
  trigger_digest: 'Trigger',
  automation_alert: 'Automation',
  api_key_expiring: 'API key',
}

const KIND_SLOT = 84
const AGE_SLOT = 72

export default function NotificationsPage() {
  const [tab, select] = useUrlTab(TABS, 'unread')
  return (
    <Box data-testid="notifications-page">
      <PageHeader title="Notifications" />
      <Tabs tabs={TABS} tab={tab} onSelect={select} label="Notifications" idPrefix="notifications" />
      {TABS.map(t => (
        <TabPanel key={t.id} id={t.id} current={tab} idPrefix="notifications">
          {/* Mounted with its tab only: each is its own read, and its own Load more. */}
          {tab === t.id && <NotificationList view={t.id} />}
        </TabPanel>
      ))}
    </Box>
  )
}

function NotificationList({ view }: { view: InboxView }) {
  const [limit, setLimit] = useState(INBOX_LIMIT)
  const { query, entries } = useInboxFeed(view, limit)
  const { open, markRead, markUnread, markAllRead } = useInboxActions()

  if (query.data === undefined) {
    return query.isError
      ? (
        <Box sx={{ pt: 1.5 }}>
          <LoadFailedState
            title="Notifications unavailable"
            error={apiDetail(query.error, 'Could not load notifications.')}
            onRetry={() => { void query.refetch() }}
          />
        </Box>
      )
      : <Box sx={{ pt: 1.5 }}><LoadingState rows={4} /></Box>
  }
  if (entries.length === 0) return <EmptyState title={view === 'unread' ? 'Nothing new' : 'No notifications'} />

  const rows = entries.map(entry => notificationRow(entry, { open, markRead, markUnread }))
  // A full page may have more behind it; the API's cap is the last page.
  const more = query.data.length >= limit && limit < INBOX_MAX
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, pt: 1.5 }}>
      {hasMarkable(entries) && (
        <Box sx={{ display: 'flex', justifyContent: 'flex-end' }}>
          {/* Not "Clear": it reads as delete. */}
          <Button size="small" variant="outlined" data-testid="notifications-mark-all" sx={tapTargetSx}
            onClick={() => markAllRead(entries)}>
            Mark all read
          </Button>
        </Box>
      )}
      <FeedList
        ariaLabel={view === 'unread' ? 'Unread notifications' : 'All notifications'}
        testId={`notifications-${view}`}
        sections={[{ key: view, rail: false, rows }]}
      />
      {more && (
        <Box sx={{ display: 'flex', justifyContent: 'center' }}>
          <Button size="small" data-testid="notifications-more" sx={tapTargetSx} disabled={query.isFetching}
            onClick={() => setLimit(next => Math.min(next + INBOX_LIMIT, INBOX_MAX))}>
            {query.isFetching ? 'Loading…' : 'Load more'}
          </Button>
        </Box>
      )}
    </Box>
  )
}

type Actions = Pick<ReturnType<typeof useInboxActions>, 'open' | 'markRead' | 'markUnread'>

/** One notification as a list row: the read state on the disc (a press flips it), what happened as the title — the
 *  subject bold, the detail after it, up to two lines — the kind and the age at the end. */
function notificationRow(entry: InboxItem, { open, markRead, markUnread }: Actions): FeedRowSpec {
  const text = describeItem(entry)
  const unread = isUnread(entry)
  const who = isCommentKind(entry.kind) && entry.author_name ? entry.author_name : null
  const titleText = [who, text.title, text.detail].filter(Boolean).join(' · ')
  const actions: OverflowItem[] = [{
    key: 'open', label: 'Open', ariaLabel: `Open ${text.title}`, testId: 'notification-open',
    onClick: () => open(entry),
  }]
  if (canMarkRead(entry)) {
    actions.push({ key: 'read', label: 'Mark as read', ariaLabel: `Mark ${text.title} as read`, testId: 'notification-mark-read', onClick: () => markRead([entry]) })
  }
  if (canMarkUnread(entry)) {
    actions.push({ key: 'unread', label: 'Mark as unread', ariaLabel: `Mark ${text.title} as unread`, testId: 'notification-mark-unread', onClick: () => markUnread([entry]) })
  }
  return {
    key: `${entry.kind}-${entry.id}`,
    indicator: indicatorOf(entry, { markRead, markUnread }),
    title: (
      <>
        {/* Off on a phone: two lines hold the subject and what was said, not an author on a line of its own. */}
        {who && <Box component="span" sx={{ color: 'text.secondary', [PHONE]: { display: 'none' } }}>{who} · </Box>}
        <Box component="span" sx={{ fontWeight: unread ? 600 : 400 }}>{text.title}</Box>
        {text.detail && <Box component="span" sx={{ color: 'text.secondary' }}> — {text.detail}</Box>}
      </>
    ),
    titleText,
    titleLines: 2,
    onRowClick: () => open(entry),
    rowClickLabel: itemTarget(entry).external ? 'Open in a new tab' : 'Open',
    data: { kind: entry.kind, id: entry.id, read: unread ? 'false' : 'true' },
    slots: [
      { key: 'kind', width: KIND_SLOT, gate: 'meta', align: 'right', dim: true, node: KIND_LABEL[entry.kind] },
      {
        key: 'age', width: AGE_SLOT, align: 'right', dim: true,
        title: entry.created_at ? absoluteTime(entry.created_at) : undefined,
        node: relativeAge(entry.created_at),
      },
    ],
    actions,
  }
}

/** The disc: ● unread, ○ read; a press marks it the other way. A pending request waits on a decision instead —
 *  it leaves the list once someone decides it. */
function indicatorOf(entry: InboxItem, { markRead, markUnread }: Pick<Actions, 'markRead' | 'markUnread'>): FeedIndicator {
  if (entry.kind === 'access_request') return { tone: 'warning', glyph: '?', label: 'Waiting for a decision' }
  if (isUnread(entry)) {
    return {
      tone: 'info', glyph: '●', label: 'Unread',
      ...(canMarkRead(entry) ? { onPress: () => markRead([entry]), pressLabel: 'Mark as read', pressGlyph: '○' } : {}),
    }
  }
  return {
    tone: 'neutral', glyph: '○', label: 'Read',
    ...(canMarkUnread(entry) ? { onPress: () => markUnread([entry]), pressLabel: 'Mark as unread', pressGlyph: '●' } : {}),
  }
}
