import { type JSX, useCallback, useState } from 'react'
import { Link as RouterLink, useParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Link from '@mui/material/Link'
import { apiDetail } from '../../../api'
import { useAuth } from '../../../auth'
import Breadcrumb from '../../../components/Breadcrumb'
import { TabPanel, Tabs, type TabDef } from '../../../components/Tabs'
import { useEditRecord } from '../../../hooks/useEditRecord'
import { useListPage, useListPageQuery } from '../../../hooks/useListPage'
import { useUrlTab } from '../../../hooks/useUrlTab'
import { absoluteTime, compactAge, relativeAge } from '../../../lib/relativeTime'
import Banner from '../../../ui/Banner'
import { DetailPage } from '../../../ui/detail/DetailPage'
import DetailStatus from '../../../ui/detail/DetailStatus'
import { AGE_SLOT } from '../../../ui/feed/geometry'
import FeedList from '../../../ui/feed/FeedList'
import type { FeedRowSpec } from '../../../ui/feed/types'
import { KeyValue, KeyValueList } from '../../../ui/KeyValueList'
import { EMAIL_SENDS_CONTAINER_SX, SENDS_NO_SOURCE, tapTargetSx } from '../../../ui/listStyles'
import ListPager from '../../../ui/ListPager'
import StatusChip from '../../../ui/StatusChip'
import {
  deleteGmailConnection,
  EMAIL_SETTINGS_URL,
  type EmailSend,
  getGmailConnection,
  type GmailConnection,
  KIND_LABEL,
  ORG_EMAIL_KEY,
  sendSource,
  sendsPath,
} from './api'
import { useGmailConnectReturn } from './connectReturn'

/* One Gmail connection: `/settings/email/:connectionId`. Overview (the mailbox, how it connected, what it can do,
 * its last use or failure) and Sent (its sent mail and refused attempts, newest first, each linked to the task or
 * automation run that sent it). Disconnect deletes the
 * connection and its secret; the sends are kept. Every rule is the server's (`app/tenant_gmail/`). Connect with
 * Google lands here with `?gmail_connect=connected`. */

type Tab = 'overview' | 'sent'

const TABS: readonly TabDef<Tab>[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'sent', label: 'Sent' },
]

const TRAIL_ROOT = { label: 'Email', to: EMAIL_SETTINGS_URL }

export default function GmailConnectionPage(): JSX.Element {
  const { connectionId } = useParams<{ connectionId: string }>()
  const orgId = useAuth().admin?.org?.id ?? ''
  const queryClient = useQueryClient()
  const [detail, setDetail] = useState<GmailConnection | null>(null)
  const [returned, , clearReturn] = useGmailConnectReturn()

  const fetchRow = useCallback((id: string) => getGmailConnection(orgId, id), [orgId])
  const { loading, notFound, loadError, deleting, deleteError, confirmAndDelete, confirmDialog } = useEditRecord(
    orgId && connectionId ? connectionId : null, fetchRow, setDetail,
    { loadFallback: 'Could not load the Gmail connection.', listTo: EMAIL_SETTINGS_URL, deleteFallback: 'Could not disconnect. Retry.' },
  )

  const [tab, selectTab] = useUrlTab(TABS, 'overview')

  if (loading || notFound || loadError || !detail) {
    return (
      <DetailStatus
        trail={<Breadcrumb items={[TRAIL_ROOT, { label: 'Gmail' }]} />}
        loading={loading || (!notFound && !loadError)}
        notFound={notFound && 'Gmail connection not found'}
        error={loadError || 'Could not load the Gmail connection.'}
      />
    )
  }

  const disconnect = () => void confirmAndDelete(
    {
      title: `Disconnect ${detail.address}?`,
      message: 'Jaah stops sending from this mailbox. Its sent-mail history is kept.',
      confirmLabel: 'Disconnect',
      destructive: true,
    },
    async () => {
      await deleteGmailConnection(orgId, detail.id)
      await queryClient.invalidateQueries({ queryKey: ORG_EMAIL_KEY })
    },
  )

  const abilities = detail.can_receive ? 'Send, receive' : 'Send'

  return (
    <DetailPage
      breadcrumb={<Breadcrumb items={[TRAIL_ROOT, { label: detail.address }]} />}
      title={detail.address}
      titleAside={detail.last_error
        ? <StatusChip label="Failing" tone="error" data-status="failing" />
        : <StatusChip label="Connected" tone="success" data-status="connected" />}
      actions={(
        <Button variant="outlined" color="error" onClick={disconnect} disabled={deleting}
          data-testid="gmail-disconnect" sx={tapTargetSx}>
          Disconnect
        </Button>
      )}
      testId="gmail-connection"
    >
      {returned?.connected && <Banner severity="success" onDismiss={clearReturn} testId="gmail-connected">Gmail connected.</Banner>}
      {deleteError && <Banner severity="error">{deleteError}</Banner>}

      <Tabs tabs={TABS} tab={tab} onSelect={selectTab} label="Gmail connection sections" idPrefix="gmail-connection" />

      <TabPanel id="overview" current={tab} idPrefix="gmail-connection">
        <Box sx={{ pt: 2 }}>
          <KeyValueList testId="gmail-connection-facts">
            <KeyValue label="Mailbox">{detail.address}</KeyValue>
            <KeyValue label="Connected by" valueTestId="gmail-connection-kind">{KIND_LABEL[detail.kind]}</KeyValue>
            <KeyValue label="Can" valueTestId="gmail-connection-abilities">{abilities}</KeyValue>
            {detail.can_receive && (
              <KeyValue label="Synced" valueTestId="gmail-connection-synced"
                title={detail.last_sync_at ? absoluteTime(detail.last_sync_at) : undefined}>
                {detail.last_sync_at ? relativeAge(detail.last_sync_at) : 'Not yet'}
              </KeyValue>
            )}
            <KeyValue label="Connected">
              {`${absoluteTime(detail.created_at)}${detail.created_by ? ` by ${detail.created_by}` : ''}`}
            </KeyValue>
            <KeyValue label="Last used" title={detail.last_used_at ? absoluteTime(detail.last_used_at) : undefined}>
              {detail.last_used_at ? relativeAge(detail.last_used_at) : 'Never'}
            </KeyValue>
            {detail.last_error && (
              <KeyValue label="Last error" color="error.main" valueTestId="gmail-connection-error">{detail.last_error}</KeyValue>
            )}
            {detail.sync_error && (
              <KeyValue label="Receiving" color="warning.main" valueTestId="gmail-connection-sync-error">
                {detail.sync_error}
              </KeyValue>
            )}
          </KeyValueList>
        </Box>
      </TabPanel>

      {tab === 'sent' && (
        <TabPanel id="sent" current={tab} idPrefix="gmail-connection">
          <SentList orgId={orgId} connectionId={detail.id} />
        </TabPanel>
      )}

      {confirmDialog}
    </DetailPage>
  )
}

const TO_SLOT = 200
const TO_NARROW = 120
const STATUS_SLOT = 72
const SOURCE_SLOT = 160

/** "ada@example.com", "ada@example.com +2", plus the Cc count when there is one. */
function recipients(send: EmailSend): string {
  const [first, ...rest] = send.to
  const to = first ? (rest.length > 0 ? `${first} +${rest.length}` : first) : '—'
  return send.cc.length > 0 ? `${to} · Cc ${send.cc.length}` : to
}

/* One connection's sends, newest first (the server's order), paged in the URL under `sent_`. */
function SentList({ orgId, connectionId }: { orgId: string; connectionId: string }): JSX.Element {
  const list = useListPage({ prefix: 'sent_' })
  const path = sendsPath(orgId, { connectionId, page: list.page, per: list.per })
  const { rows: sends, total, loading, error } = useListPageQuery<EmailSend>({ queryKey: ['org-email-sends', path], path })

  const rows: FeedRowSpec[] = sends.map((send): FeedRowSpec => {
    const failed = send.status === 'failed'
    const subject = send.subject || '(no subject)'
    const source = sendSource(send)
    return {
      key: send.id,
      indicator: failed
        ? { tone: 'error', glyph: '✕', label: 'Failed' }
        : { tone: 'success', glyph: '✓', label: 'Sent' },
      title: subject,
      titleText: subject,
      titleWrapAt: SENDS_NO_SOURCE,
      notes: failed && send.error ? [{ key: 'error', tone: 'error', text: send.error }] : [],
      data: { status: send.status },
      // The source column drops on a phone and a narrow list; the ⋯ menu is its second home there.
      actions: source ? [{ key: 'source', label: source.verb, ariaLabel: `${source.verb}: ${source.label}`, to: source.to, testId: 'gmail-send-open-source' }] : undefined,
      slots: [
        {
          // Who it went to stays on a phone and a narrow list, narrower; the status chip repeats the row's indicator and goes first.
          key: 'to', width: TO_SLOT, phoneWidth: TO_NARROW, narrowWidth: { query: SENDS_NO_SOURCE, width: TO_NARROW }, align: 'right', dim: true,
          title: [...send.to, ...send.cc.map(cc => `Cc ${cc}`)].join(', '), node: recipients(send), testId: 'gmail-send-to',
        },
        {
          key: 'status', width: STATUS_SLOT, gate: 'sendsStatus', align: 'right', title: failed ? (send.error ?? 'Failed') : 'Sent',
          node: <StatusChip label={failed ? 'Failed' : 'Sent'} tone={failed ? 'error' : 'success'} />,
          testId: 'gmail-send-status',
        },
        {
          key: 'source', width: SOURCE_SLOT, gate: 'sendsSource', align: 'right', title: source?.title, testId: 'gmail-send-source',
          node: source && (
            <Link component={RouterLink} to={source.to} underline="hover" noWrap data-testid="gmail-send-source-link">
              {source.label}
            </Link>
          ),
        },
        {
          key: 'age', width: AGE_SLOT, align: 'right', mono: true, dim: true,
          title: absoluteTime(send.created_at), node: compactAge(send.created_at),
        },
      ],
    }
  })

  return (
    // The query container `SENDS_NO_*` read: the list's own width, not the window's (beside an open task or run).
    <Box sx={{ pt: 2, ...EMAIL_SENDS_CONTAINER_SX }} data-testid="gmail-sends">
      {error && <Banner severity="error">{apiDetail(error, 'Failed to load the sent mail.')}</Banner>}
      <FeedList
        ariaLabel="Sent mail"
        loading={loading}
        emptyMessage="Nothing sent yet."
        sections={rows.length > 0 ? [{ key: 'sends', rows }] : []}
      />
      <ListPager page={list.page} per={list.per} total={total} onPage={list.setPage} onPer={list.setPer} testId="gmail-sends-pager" />
    </Box>
  )
}
