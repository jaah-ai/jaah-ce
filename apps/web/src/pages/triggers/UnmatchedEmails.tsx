import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import EmptyState from '../../ui/EmptyState'
import { useApiRead } from '../../hooks/useApiRead'
import { absoluteTime, compactAge } from '../../lib/relativeTime'
import { FONT_SIZE } from '../../theme'
import Banner from '../../ui/Banner'
import FeedList from '../../ui/feed/FeedList'
import { AGE_SLOT } from '../../ui/feed/geometry'
import type { FeedRowSpec } from '../../ui/feed/types'
import { usePhone } from '../../hooks/usePhone'
import { tapTargetSx } from '../../ui/listStyles'
import SectionCard from '../../ui/SectionCard'
import StatusChip from '../../ui/StatusChip'
import { listUnmatched } from './api'

/* UNMATCHED EMAILS (architecture §4 "Gmail (C6)"): the caller's OWN org's inbound mail that no email trigger's rule
 * took, so it filed nothing, plus mail refused before routing (SES spam or virus verdict, DMARC fail under p=reject, a
 * sender over its daily cap, an org whose plan has no Tenant email), marked Rejected with its reason. The server
 * answers with the caller's own org's rows whatever the org filter says (an org admin sees their org's, the operator
 * org zero's), so the read goes past the filter. Two homes: the operator's Automations list
 * (`pages/automations/AutomationList.tsx`) and an org's Settings › Organizations › Email › Inbound
 * (`pages/settings/email/EmailSection.tsx`, `framed={false}`: the tab names it, so no card of its own). Re-read every
 * 30 s, the `ses-inbound` cadence, so new mail shows without Refresh (nothing on the event stream announces it). Newest
 * first, one `ui/feed/FeedList` row each: the subject WRAPS, never ellipsized, so a long one reads whole; a Rejected
 * chip, its reason, the sender and the age in slots. The reason's slot is off the phone band only (`gate: 'phone'`),
 * where the reason goes under the subject instead: the row already grows with its subject, and has no width to
 * spare. Framed (Automations), its "nothing here" is the feed's plain line, not `EmptyState`: a populated Automations
 * page must show no empty state. Unframed (the Inbound tab), an empty list is one inline line beside Refresh, not the
 * feed's tall centred block far below it. */

const LIMIT = 20
/** `ses-inbound`'s cadence (`app/cron_system_activities.py::SES_INBOUND_INTERVAL_SECONDS`): no sooner can new mail land. */
const POLL_MS = 30_000

/** `rejected_reason` → its short reason; an unknown value shows raw. */
const REJECTED_REASON: Record<string, string> = {
  spam: 'Spam',
  virus: 'Virus',
  dmarc: 'Failed DMARC',
  rate_limited: 'Too many from this sender',
  plan_off: 'Plan has no Tenant email',
}

const REJECTED_SLOT = 76
const REASON_SLOT = 170
const SENDER_SLOT = 200
const SENDER_NARROW = 110
/** The reason under the subject on a phone, in the slot's own grey and size. */
const REASON_UNDER_SX = { display: 'block', color: 'text.disabled', fontSize: FONT_SIZE.xs } as const

export default function UnmatchedEmails({ framed = true }: { framed?: boolean }) {
  const { data, loading, error, reload } = useApiRead(
    ['unmatched-emails'],
    () => listUnmatched('gmail', LIMIT),
    'Failed to load unmatched emails',
    { refetchInterval: POLL_MS },
  )
  const rows = data ?? []
  const phone = usePhone({ noSsr: true })

  const feedRows: FeedRowSpec[] = rows.map((row): FeedRowSpec => {
    const subject = row.subject || '(no subject)'
    const reason = row.rejected_reason ? (REJECTED_REASON[row.rejected_reason] ?? row.rejected_reason) : null
    const sender = row.sender || 'Unknown sender'
    return {
      key: String(row.id),
      indicator: reason
        ? { tone: 'warning', glyph: '✕', label: 'Rejected', title: `Rejected: ${reason}` }
        : { tone: 'neutral', glyph: '○', label: 'Unmatched' },
      title: reason && phone ? <>{subject}<Box component="span" sx={REASON_UNDER_SX}>{reason}</Box></> : subject,
      titleText: subject,
      href: null,
      titleLines: 'all',
      slots: [
        {
          key: 'rejected', width: REJECTED_SLOT, align: 'right', testId: reason ? 'unmatched-email-rejected' : undefined,
          node: reason ? <StatusChip label="Rejected" tone="warning" data-status="rejected" /> : null,
        },
        { key: 'reason', width: REASON_SLOT, gate: 'phone', align: 'left', dim: true, node: reason },
        /* Two lines and the full address on hover, so a long sender is never silently cut. */
        { key: 'sender', width: SENDER_SLOT, phoneWidth: SENDER_NARROW, align: 'right', dim: true, lines: 2, title: sender, node: sender },
        {
          key: 'age', width: AGE_SLOT, align: 'right', mono: true, dim: true,
          title: row.received_at ? absoluteTime(row.received_at) : undefined, node: row.received_at ? compactAge(row.received_at) : null,
        },
      ],
    }
  })

  const refreshButton = (
    <Button size="small" onClick={() => void reload()} sx={tapTargetSx}>
      Refresh
    </Button>
  )
  /* Loaded, unbroken and empty: the unframed tab says so on Refresh's own line. */
  const emptyInline = !framed && !error && data != null && rows.length === 0
  const body = (
    <>
      {error && <Banner severity="error">{error}</Banner>}
      {/* Under a load error the list says nothing: "No unmatched emails." would be a claim the read never made. */}
      {(error && rows.length === 0) || emptyInline ? null : (
        <FeedList
          ariaLabel="Unmatched emails"
          testId="unmatched-emails-list"
          loading={loading && data === null}
          skeletonRows={3}
          emptyMessage="No unmatched emails."
          sections={feedRows.length > 0 ? [{ key: 'unmatched', rows: feedRows, rail: false }] : []}
        />
      )}
    </>
  )

  if (!framed) {
    return (
      <Box data-testid="unmatched-emails" sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
        <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 1 }}>
          {emptyInline ? <EmptyState inline title="No unmatched emails." /> : <span />}
          {refreshButton}
        </Box>
        {body}
      </Box>
    )
  }
  return (
    <Box data-testid="unmatched-emails" sx={{ mt: 3 }}>
      <SectionCard title="Unmatched emails" actions={refreshButton}>
        {body}
      </SectionCard>
    </Box>
  )
}
