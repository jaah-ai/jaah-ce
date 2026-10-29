import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { useApiRead } from '../../hooks/useApiRead'
import { relativeAge } from '../../lib/relativeTime'
import { formatZoned } from '../../lib/zonedTime'
import Banner from '../../ui/Banner'
import { tapTargetSx } from '../../ui/listStyles'
import LoadingState from '../../ui/LoadingState'
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
 * first; the subject WRAPS, never ellipsized, so a long one reads whole. Its "nothing here" is a plain line, not
 * `EmptyState`: a populated Automations page must show no empty state. */

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

export default function UnmatchedEmails({ framed = true }: { framed?: boolean }) {
  const { data, loading, error, reload } = useApiRead(
    ['unmatched-emails'],
    () => listUnmatched('gmail', LIMIT),
    'Failed to load unmatched emails',
    { refetchInterval: POLL_MS },
  )
  const rows = data ?? []

  const refreshButton = (
    <Button size="small" onClick={() => void reload()} sx={tapTargetSx}>
      Refresh
    </Button>
  )
  const body = (
    <>
      {error && <Banner severity="error">{error}</Banner>}
      {loading && data === null ? (
        <LoadingState rows={3} />
      ) : error && rows.length === 0 ? null : rows.length === 0 ? (
        <Typography variant="body2" data-testid="unmatched-emails-none" sx={{ color: 'text.secondary' }}>
          No unmatched emails.
        </Typography>
      ) : (
        <Box component="ul" aria-label="Unmatched emails" sx={{ listStyle: 'none', m: 0, p: 0 }}>
          {rows.map(row => (
            <Box
              component="li"
              key={row.id}
              data-testid="unmatched-email"
              sx={{ py: 1, borderTop: 1, borderColor: 'divider', '&:first-of-type': { borderTop: 0 }, minWidth: 0 }}
            >
              <Typography variant="body2" sx={{ fontWeight: 600, overflowWrap: 'anywhere' }}>
                {row.subject || '(no subject)'}
              </Typography>
              {row.rejected_reason && (
                <Box
                  data-testid="unmatched-email-rejected"
                  sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 0.75, rowGap: 0.25, my: 0.25, minWidth: 0 }}
                >
                  <StatusChip label="Rejected" tone="warning" data-status="rejected" />
                  <Typography variant="caption" sx={{ color: 'text.secondary', overflowWrap: 'anywhere', minWidth: 0 }}>
                    {REJECTED_REASON[row.rejected_reason] ?? row.rejected_reason}
                  </Typography>
                </Box>
              )}
              <Typography variant="caption" component="p" sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}>
                {row.sender || 'Unknown sender'}
                {row.received_at && (
                  <Box component="span" title={formatZoned(row.received_at)}>
                    {' · '}
                    {relativeAge(row.received_at)}
                  </Box>
                )}
              </Typography>
            </Box>
          ))}
        </Box>
      )}
    </>
  )

  if (!framed) {
    return (
      <Box data-testid="unmatched-emails" sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
        <Box sx={{ display: 'flex', justifyContent: 'flex-end' }}>{refreshButton}</Box>
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
