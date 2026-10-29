import { useState } from 'react'
import { Link, Navigate, useSearchParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Paper from '@mui/material/Paper'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { apiDetail } from '../api'
import { useAuth } from '../auth'
import { TabPanel, Tabs, type TabDef } from '../components/Tabs'
import { useRowActions } from '../hooks/useBusyIds'
import { useUrlTab } from '../hooks/useUrlTab'
import {
  approveRequest,
  denyRequest,
  listRequests,
  managesAccess,
  REQUESTS_STALE_MS,
  scopeHref,
  scopeLabel,
  type AccessRequest,
  type RequestBox,
} from '../lib/accessRequests'
import { relativeAge } from '../lib/relativeTime'
import Banner from '../ui/Banner'
import EmptyState from '../ui/EmptyState'
import LoadFailedState from '../ui/LoadFailedState'
import LoadingState from '../ui/LoadingState'
import { StatusChip, type Tone } from '../ui/StatusChip'

/* The Requests inbox (access-model C10; the rules are `apps/api/app/access_requests.py`'s) — Support's
 * Requests tab (`pages/support/SupportPage.tsx`, `/support?tab=requests`), its box on a second strip
 * (`?box=`, since Support's strip owns `?tab=`).
 * Pending: what the caller may decide — Approve (optional expiry, default never, A2) or Deny. Mine: the
 * caller's own asks, every state. History: what the caller may decide, decided. Every signed-in user has a
 * Mine box, so the tab is offered to all. */

const TABS: readonly TabDef<RequestBox>[] = [
  { id: 'pending', label: 'Pending' },
  { id: 'mine', label: 'Mine' },
  { id: 'history', label: 'History' },
]

const STATE_TONE: Record<AccessRequest['state'], Tone> = { pending: 'info', approved: 'success', denied: 'neutral' }
const STATE_LABEL: Record<AccessRequest['state'], string> = { pending: 'Pending', approved: 'Approved', denied: 'Denied' }

/** The end of `day` (a `yyyy-mm-dd` date input value) in the viewer's zone, as ISO; null for none. */
function endOfDay(day: string): string | null {
  if (!day) return null
  const at = new Date(`${day}T23:59:59`)
  return Number.isNaN(at.getTime()) ? null : at.toISOString()
}

function todayInput(): string {
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
}

/** The retired `/requests` address (bell links, bookmarks): one `replace` hop onto Support's Requests tab,
 *  its `?tab=<box>` carried as `?box=`, other params kept. */
export function RequestsRedirect() {
  const [searchParams] = useSearchParams()
  const params = new URLSearchParams(searchParams)
  const box = params.get('tab')
  params.set('tab', 'requests')
  if (box !== null) params.set('box', box)
  return <Navigate to={`/support?${params.toString()}`} replace />
}

export default function Requests() {
  const { admin } = useAuth()
  // The box lives in the URL (hooks/useUrlTab.ts), under `box`: Support's strip owns `tab`.
  const [tab, select] = useUrlTab(TABS, managesAccess(admin) ? 'pending' : 'mine', 'box')

  return (
    <Box data-testid="requests-page">
      <Tabs tabs={TABS} tab={tab} onSelect={select} label="Request boxes" idPrefix="requests" />
      {TABS.map(t => (
        <TabPanel key={t.id} id={t.id} current={tab} idPrefix="requests">
          {/* Mounted with its tab only: each box is its own read. */}
          {tab === t.id && <RequestList box={t.id} />}
        </TabPanel>
      ))}
    </Box>
  )
}

const EMPTY: Record<RequestBox, string> = {
  pending: 'Nothing to decide',
  mine: 'No requests',
  history: 'Nothing decided yet',
}

function RequestList({ box }: { box: RequestBox }) {
  const query = useQuery({ queryKey: ['access-requests', box], queryFn: () => listRequests(box), staleTime: REQUESTS_STALE_MS, refetchInterval: 60_000 })
  if (query.data === undefined) {
    return query.isError
      ? (
        <Box sx={{ pt: 1.5 }}>
          <LoadFailedState title="Requests unavailable" error={apiDetail(query.error, 'Could not load requests.')} onRetry={() => { void query.refetch() }} />
        </Box>
      )
      : <Box sx={{ pt: 1.5 }}><LoadingState rows={3} /></Box>
  }
  if (query.data.length === 0) return <EmptyState title={EMPTY[box]} scoped />
  return (
    <Box data-testid={`requests-${box}`} sx={{ display: 'flex', flexDirection: 'column', gap: 1, pt: 1.5 }}>
      {query.data.map(row => (
        <RequestRow key={row.id} row={row} box={box} />
      ))}
    </Box>
  )
}

function RequestRow({ row, box }: { row: AccessRequest; box: RequestBox }) {
  const queryClient = useQueryClient()
  const [expiry, setExpiry] = useState('')
  const { busy, error, run } = useRowActions<'approve' | 'deny'>()
  const href = row.state === 'approved' ? scopeHref(row.scope) : null

  const decide = (verdict: 'approve' | 'deny') =>
    run(verdict, 'The decision was not recorded.', async () => {
      if (verdict === 'approve') await approveRequest(row.id, endOfDay(expiry))
      else await denyRequest(row.id)
      // Every box and the bell: a decision moves the row out of Pending and off every approver's bell.
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['access-requests'] }),
        queryClient.invalidateQueries({ queryKey: ['inbox'] }),
      ])
    })

  const who = box === 'mine' ? 'You' : row.requester.name || 'Someone'
  return (
    <Paper
      variant="outlined"
      data-testid="request-row"
      data-state={row.state}
      sx={{ p: { xs: 1.25, sm: 1.5 }, display: 'flex', flexDirection: 'column', gap: 1 }}
    >
      <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 1, flexWrap: 'wrap' }}>
        <Typography variant="body2" sx={{ flex: '1 1 16rem', minWidth: 0, overflowWrap: 'anywhere' }}>
          <Box component="span" sx={{ fontWeight: 600 }}>{who}</Box>
          {box === 'mine' || row.state !== 'pending' ? ' asked for ' : ' asks for '}
          <Box component="span" sx={{ fontWeight: 600 }}>{row.role.name}</Box>
          {' on '}
          {href ? <Link to={href}>{scopeLabel(row.scope)}</Link> : scopeLabel(row.scope)}
        </Typography>
        <StatusChip label={STATE_LABEL[row.state]} tone={STATE_TONE[row.state]} />
        <Typography variant="caption" sx={{ color: 'text.secondary', fontVariantNumeric: 'tabular-nums' }}>
          {relativeAge(row.decided_at ?? row.created_at)}
        </Typography>
      </Box>
      {row.note && (
        <Typography
          variant="body2"
          data-testid="request-note"
          sx={{ color: 'text.secondary', whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}
        >
          “{row.note}”
        </Typography>
      )}
      {row.decided_by && (
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>
          {STATE_LABEL[row.state]} by {row.decided_by.name}
        </Typography>
      )}
      {box === 'pending' && row.state === 'pending' && (
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
          <TextField
            type="date"
            label="Expires"
            size="small"
            value={expiry}
            onChange={e => setExpiry(e.target.value)}
            helperText={expiry ? undefined : 'Never'}
            data-testid="request-expiry"
            slotProps={{ inputLabel: { shrink: true }, htmlInput: { min: todayInput() } }}
            sx={{ width: '11rem' }}
          />
          <Button
            variant="contained"
            onClick={() => void decide('approve')}
            disabled={busy.size > 0}
            data-testid="request-approve"
            sx={{ minHeight: 44 }}
          >
            {busy.has('approve') ? 'Approving…' : 'Approve'}
          </Button>
          <Button
            variant="outlined"
            onClick={() => void decide('deny')}
            disabled={busy.size > 0}
            data-testid="request-deny"
            sx={{ minHeight: 44 }}
          >
            {busy.has('deny') ? 'Denying…' : 'Deny'}
          </Button>
        </Box>
      )}
      {error && <Banner severity="error">{error}</Banner>}
    </Paper>
  )
}
