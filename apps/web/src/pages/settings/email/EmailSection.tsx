import { type JSX, useState } from 'react'
import { Link as RouterLink, useNavigate } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Link from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import { apiDetail } from '../../../api'
import { type TabDef, TabPanel, Tabs } from '../../../components/Tabs'
import { useConfirm } from '../../../components/ConfirmDialog'
import { useSubmit } from '../../../hooks/useSubmit'
import { useUrlTab } from '../../../hooks/useUrlTab'
import { navigateExternal } from '../../../lib/externalNavigate'
import { absoluteTime, relativeAge } from '../../../lib/relativeTime'
import { safeHref } from '../../../lib/safeHref'
import Banner from '../../../ui/Banner'
import { CopyButton } from '../../../ui/CopyButton'
import EmptyState from '../../../ui/EmptyState'
import FeedList from '../../../ui/feed/FeedList'
import type { FeedRowSpec } from '../../../ui/feed/types'
import { monoSx, tapTargetSx } from '../../../ui/listStyles'
import LoadingState from '../../../ui/LoadingState'
import { TONE_COLOR } from '../../../ui/statusTone'
import UnmatchedEmails from '../../triggers/UnmatchedEmails'
import {
  type GmailConnection,
  gmailConnectionUrl,
  getOrgEmail,
  KIND_LABEL,
  ORG_EMAIL_KEY,
  revokeTenantAddress,
  startGmailConnect,
  type TenantAddress,
} from './api'
import { useGmailConnectReturn } from './connectReturn'
import NewAddressDialog from './NewAddressDialog'
import ServiceAccountDialog from './ServiceAccountDialog'

/* Settings › Organizations › Email: one card of several lists of the org's mail, so a strip
 * (`?mail=`, the style guide's sub-tabs) rather than stacked cards inside Settings' own card:
 * - Connections: the Gmail mailboxes the org sends from (a service account also receives; the kind's hover says
 *   when it last read). A row opens the connection (`GmailConnectionPage.tsx`). Connect with Google is offered only
 *   while the operator set up the Google client; a service account always (on the plan).
 * - Addresses: the hosted addresses the org receives at; each feeds the one automation whose email trigger holds it,
 *   else its mail lands in Inbound. New is a one-shot act (a dialog: an optional label); Revoke confirms.
 * - Inbound: the org's unmatched and rejected mail (`triggers/UnmatchedEmails.tsx`), mounted only while open so a
 *   hidden tab does not poll.
 * Off the plan every list still shows, so an org keeps its history and can remove a secret or an address — nothing
 * new is made. A panel renders only while its tab is open: each has its own empty state. */

type MailTab = 'connections' | 'addresses' | 'inbound'
const TABS: readonly TabDef<MailTab>[] = [
  { id: 'connections', label: 'Connections' },
  { id: 'addresses', label: 'Addresses' },
  { id: 'inbound', label: 'Inbound' },
]

const KIND_SLOT = 128
const LAST_USED_SLOT = 140
const FEEDS_SLOT = 180

/** A connection's receiving, in words: a service account reads its mailbox; Google sign-in only sends. */
function receiveFact(row: GmailConnection): string {
  if (!row.can_receive) return 'Send only'
  return row.last_sync_at ? `Receiving · synced ${relativeAge(row.last_sync_at)}` : 'Receiving · not synced yet'
}

/** "4m ago", "Never used" — or, after a failed send, that it failed (the reason is the row's hover). */
function lastUsedNode(row: GmailConnection): JSX.Element | string {
  if (row.last_error) return <Box component="span" sx={{ color: TONE_COLOR.error }}>Send failed</Box>
  return row.last_used_at ? relativeAge(row.last_used_at) : 'Never used'
}

export function EmailSection({ orgId }: { orgId: string }): JSX.Element {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const query = useQuery({ queryKey: [...ORG_EMAIL_KEY, orgId], queryFn: () => getOrgEmail(orgId), refetchOnMount: 'always' })
  const [tab, selectTab] = useUrlTab(TABS, 'connections', 'mail')
  const [adding, setAdding] = useState(false)
  const { busy, formError, run } = useSubmit()
  const [, returnedError, clearReturn] = useGmailConnectReturn()

  const email = query.data
  const loadError = query.error ? apiDetail(query.error, 'Failed to load email settings.') : ''

  const startGoogle = () =>
    run('Could not start Connect with Google.', async () => {
      clearReturn()
      const { authorize_url } = await startGmailConnect()
      const href = safeHref(authorize_url)
      if (!href) throw new Error('The Google link the server returned is not a usable URL.')
      navigateExternal(href)
    })

  const connected = (row: GmailConnection) => {
    setAdding(false)
    void queryClient.invalidateQueries({ queryKey: ORG_EMAIL_KEY })
    navigate(gmailConnectionUrl(row.id))
  }

  const actions = email?.on_plan ? (
    <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1 }}>
      {email.oauth_available && (
        <Button variant="contained" size="small" disabled={busy} onClick={() => void startGoogle()}
          data-testid="gmail-connect-google" sx={tapTargetSx}>
          Connect with Google
        </Button>
      )}
      <Button variant={email.oauth_available ? 'outlined' : 'contained'} size="small" disabled={busy}
        onClick={() => setAdding(true)} data-testid="gmail-add-sa" sx={tapTargetSx}>
        Add service account
      </Button>
    </Box>
  ) : undefined

  const rows: FeedRowSpec[] = (email?.connections ?? []).map((row): FeedRowSpec => ({
    key: row.id,
    indicator: row.last_error
      ? { tone: 'error', glyph: '!', label: 'Last send failed' }
      : row.sync_error
        ? { tone: 'warning', glyph: '!', label: 'Receiving failed' }
        : { tone: 'success', glyph: '●', label: 'Connected' },
    title: <Box component="span" sx={{ fontWeight: 600 }}>{row.address}</Box>,
    titleText: row.address,
    href: gmailConnectionUrl(row.id),
    notes: [
      ...(row.last_error ? [{ key: 'error', tone: 'error' as const, text: row.last_error }] : []),
      ...(row.sync_error ? [{ key: 'sync-error', tone: 'warning' as const, text: row.sync_error }] : []),
    ],
    data: { kind: row.kind },
    slots: [
      {
        key: 'kind', width: KIND_SLOT, gate: 'meta', align: 'right', dim: true,
        title: `${KIND_LABEL[row.kind]} · ${receiveFact(row)}`, node: KIND_LABEL[row.kind], testId: 'gmail-connection-kind',
      },
      {
        // Narrower on a phone, so the address keeps the width it needs to read whole.
        key: 'last-used', width: LAST_USED_SLOT, phoneWidth: 88, align: 'right', dim: !row.last_error,
        title: row.last_error ?? (row.last_used_at ? `Last used ${absoluteTime(row.last_used_at)}` : 'Never used'),
        node: lastUsedNode(row), testId: 'gmail-connection-last-used',
      },
    ],
  }))

  const empty = email !== undefined && email.connections.length === 0

  return (
    <Box component="section" data-testid="org-email-section" sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
      {returnedError && <Banner severity="error" onDismiss={clearReturn} testId="gmail-connect-return">{returnedError}</Banner>}
      <Tabs tabs={TABS} tab={tab} onSelect={selectTab} label="Email sections" idPrefix="org-email" />
      {loadError && <Banner severity="error">{loadError}</Banner>}
      {query.isPending && <LoadingState rows={2} />}

      {tab === 'connections' && (
        <TabPanel id="connections" current={tab} idPrefix="org-email">
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
            {!empty && actions && <Box sx={{ display: 'flex', justifyContent: 'flex-end' }}>{actions}</Box>}
            {formError && <Banner severity="error">{formError}</Banner>}
            {empty && <EmptyState title="No Gmail connected." action={actions} />}
            {email !== undefined && rows.length > 0 && (
              <FeedList ariaLabel="Gmail connections" testId="gmail-connections" sections={[{ key: 'connections', rows }]} />
            )}
          </Box>
        </TabPanel>
      )}
      {tab === 'addresses' && email !== undefined && (
        <TabPanel id="addresses" current={tab} idPrefix="org-email">
          <Addresses orgId={orgId} addresses={email.addresses} canCreate={email.on_plan && email.mail_domain !== ''}
            why={email.mail_domain === '' ? 'This deployment doesn’t receive email yet.'
              : email.on_plan ? null : 'Plan has no Tenant email: no new addresses.'} />
        </TabPanel>
      )}
      {tab === 'inbound' && (
        <TabPanel id="inbound" current={tab} idPrefix="org-email">
          <UnmatchedEmails framed={false} />
        </TabPanel>
      )}
      {adding && <ServiceAccountDialog orgId={orgId} onClose={() => setAdding(false)} onConnected={connected} />}
    </Box>
  )
}

/** The Addresses tab: the hosted addresses, New (a label dialog) and Revoke (a confirm). `why` says why nothing new
 *  can be made, when so. */
function Addresses({ orgId, addresses, canCreate, why }: {
  orgId: string
  addresses: TenantAddress[]
  canCreate: boolean
  why: string | null
}): JSX.Element {
  const queryClient = useQueryClient()
  const [confirm, confirmDialog] = useConfirm()
  const [creating, setCreating] = useState(false)
  const [created, setCreated] = useState<TenantAddress | null>(null)
  const refresh = () => void queryClient.invalidateQueries({ queryKey: ORG_EMAIL_KEY })

  const revoke = async (row: TenantAddress) => {
    const done = await confirm({
      title: 'Revoke address?',
      identity: row.address,
      message: `Mail sent to it will no longer arrive.${row.automation_id
        ? ` Automation “${row.automation_name ?? 'Unnamed'}” stops receiving.` : ''} Mail already received is kept.`,
      confirmLabel: 'Revoke',
      destructive: true,
      run: { action: () => revokeTenantAddress(orgId, row.id), failed: 'Could not revoke the address.', busyLabel: 'Revoking…' },
    })
    if (!done) return
    if (created?.id === row.id) setCreated(null)
    refresh()
  }

  const newButton = canCreate ? (
    <Button variant="contained" size="small" onClick={() => setCreating(true)} data-testid="email-address-new" sx={tapTargetSx}>
      New address
    </Button>
  ) : undefined

  const rows: FeedRowSpec[] = addresses.map((row): FeedRowSpec => {
    const used = row.automation_id !== null
    const labelled = row.name.trim() !== '' && row.name !== row.address
    return {
      key: row.id,
      indicator: used ? { tone: 'success', glyph: '●', label: 'In use' } : { tone: 'neutral', glyph: '○', label: 'Not used' },
      /* The address is the thing to read and copy: whole on every width, broken anywhere on a phone. Its label rides
         beside it, never a column a narrow window drops, so two addresses always tell apart. */
      title: (
        <>
          <Box component="span" sx={{ ...monoSx, userSelect: 'text', overflowWrap: 'anywhere' }}>{row.address}</Box>
          {labelled && (
            <Box component="span" data-testid="email-address-label"
              sx={{ display: 'block', color: 'text.secondary', fontSize: '0.8125rem', overflowWrap: 'anywhere' }}>
              {row.name}
            </Box>
          )}
        </>
      ),
      titleText: labelled ? `${row.address} · ${row.name}` : row.address,
      titleLines: 'all',
      titleAside: <CopyButton variant="icon" text={() => row.address} label="Copy address" testId="email-address-copy" />,
      notes: used ? [] : [{ key: 'unused', tone: 'muted', text: 'Not used — mail goes to Unmatched' }],
      data: { address: row.address },
      actions: [{ key: 'revoke', label: 'Revoke', ariaLabel: `Revoke ${row.address}`, danger: true, testId: 'email-address-revoke', onClick: () => void revoke(row) }],
      slots: [
        {
          key: 'feeds', width: FEEDS_SLOT, phoneWidth: 96, align: 'right', dim: !used, testId: 'email-address-feeds',
          title: used ? `Feeds ${row.automation_name ?? 'an automation'}` : 'Not used — mail goes to Unmatched',
          node: used ? (
            <Link component={RouterLink} to={`/automations/${encodeURIComponent(row.automation_id ?? '')}`} underline="hover" noWrap>
              {row.automation_name ?? 'Automation'}
            </Link>
          ) : 'Not used',
        },
      ],
    }
  })

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
      {created && (
        <Banner severity="success" onDismiss={() => setCreated(null)} testId="email-address-created"
          action={<CopyButton variant="text" text={() => created.address} label="Copy" />}>
          Created <Box component="span" sx={{ ...monoSx, userSelect: 'text' }}>{created.address}</Box>
        </Banner>
      )}
      {(why || (newButton && rows.length > 0)) && (
        <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', justifyContent: 'space-between', gap: 1 }}>
          {why && <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="email-address-why">{why}</Typography>}
          {rows.length > 0 && newButton && <Box sx={{ ml: 'auto' }}>{newButton}</Box>}
        </Box>
      )}
      {rows.length === 0
        ? <EmptyState title="No addresses." action={newButton} />
        : <FeedList ariaLabel="Addresses" testId="email-addresses" sections={[{ key: 'addresses', rows }]} />}
      {creating && (
        <NewAddressDialog orgId={orgId} onClose={() => setCreating(false)}
          onCreated={row => { setCreating(false); setCreated(row); refresh() }} />
      )}
      {confirmDialog}
    </Box>
  )
}

export default EmailSection
