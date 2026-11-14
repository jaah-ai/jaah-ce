import { useEffect, useId, useRef, useState, type ComponentType, type JSX, type KeyboardEvent } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { ApiError, apiDetail, del, get, patch, post } from '../api'
import { useAuth } from '../auth'
import type { ConnectFlowProps } from '../hooks/useLinkCodeFlow'
import { isEnabled } from '../lib/editionFeatures'
import { editionModule } from '../lib/editionModule'
import type {
  NotificationAddOption,
  NotificationCatalog,
  NotificationChannel,
  NotificationChannelsDocument,
  NotificationLevel,
} from '../types'
import { FONT_WEIGHT, RADIUS, SIZE, grid } from '../theme'
import Banner from '../ui/Banner'
import BusyButton from '../ui/BusyButton'
import { DetailSection } from '../ui/detail/DetailPage'
import { DisabledReason } from '../ui/DisabledReason'
import EmptyState from '../ui/EmptyState'
import FormDialog from '../ui/FormDialog'
import FormTextField from '../ui/FormTextField'
import { ChevronRightIcon } from '../ui/icons'
import { PHONE, ROW_ACTION_SX, UNSTYLED_BUTTON_SX, anywhereSx, tapTargetSx } from '../ui/listStyles'
import LoadFailedState from '../ui/LoadFailedState'
import LoadingState from '../ui/LoadingState'
import { OverflowMenu, type OverflowItem } from '../ui/OverflowMenu'
import SegmentedControl, { type SegmentedOption } from '../ui/SegmentedControl'
import StatusChip, { type Tone } from '../ui/StatusChip'
import { danger, useConfirm } from './ConfirmDialog'
import NonBaaWarning from './NonBaaWarning'

/* THE ALERTS PANEL, shared by `/profile?tab=notifications` (the signed-in membership) and `/users/:id` > Notifications
 * (`userId`). Self-loading and self-saving: `GET …/notification-channels` answers `{channels, catalog, add_options}`
 * (`app/routes/notification_channels.py`), and every PATCH/DELETE answers the same document, which replaces the
 * cache. No host draft, no Save: each switch saves at once, applied optimistically and rolled back (with a banner)
 * when refused.
 *
 * A list of channels (one destination each: the type word, the FULL label — wrapped, never cut — and a status chip
 * when it cannot deliver). A row opens its alert settings beneath it: one Off/Brief/Full switch per catalog group, in
 * catalog order, showing the group's events' `effective` levels (none picked when they differ); a group's label,
 * chevroned, opens one switch per event. Setting a group writes `{group, level}` (the API drops the group's event
 * choices), an event `{event, level}`. Remove asks first, naming the destination.
 *
 * ONE Add channel button, a menu driven by `add_options`: an unavailable option with a reason is listed disabled
 * with it, one with no reason is not listed. Slack (a DM) and Telegram start their Connect flows
 * (`components/SlackConnect`, `components/TelegramConnect`), Slack channel its picker (`components/SlackChannelPicker`,
 * Owners and Admins only), each edition-gated, so read through `lib/editionModule`; the list reloads once the chat is
 * linked or the channel added. Those flows are the signed-in person's own, so on `/users/:id` the API lists them
 * disabled.
 *
 * EMAIL: Add ▸ Email opens a one-field dialog (prefilled with the login email on one's own panel when it has no
 * account-email channel); `POST …` answers the new channel, `unverified`, and mails a link and a 6-digit code. An
 * unverified email row shows a verify strip under its line, open or not: the code field, Confirm (`POST …/{id}/verify`)
 * and Resend (`POST …/{id}/resend`). On the account-email row that send reads Verify until it has mailed a code.
 * Every success refetches the document. Who may add and verify is the `email`/`address` option's `available`; on
 * another person's panel that is the operator only, so nobody else sees the strip. */

type FlowView = ComponentType<ConnectFlowProps>
const SlackFlow = editionModule(isEnabled('slack'),
  () => import.meta.glob<FlowView>('./SlackConnect.tsx', { eager: true, import: 'default' }))
const SlackChannelFlow = editionModule(isEnabled('slack'),
  () => import.meta.glob<FlowView>('./SlackChannelPicker.tsx', { eager: true, import: 'default' }))
const TelegramFlow = editionModule(isEnabled('telegram'),
  () => import.meta.glob<FlowView>('./TelegramConnect.tsx', { eager: true, import: 'default' }))

/** An Add channel option's flow key: its type and kind. */
type FlowKey = `${NotificationAddOption['type']}-${NotificationAddOption['kind']}`
const flowKey = (option: NotificationAddOption): FlowKey => `${option.type}-${option.kind}`

/** The flows this build can start, by option. Email is the add dialog (`AddEmailDialog`), not a flow. */
const FLOWS: Partial<Record<FlowKey, FlowView>> = {
  ...(SlackFlow ? { 'slack-dm': SlackFlow } : {}),
  ...(SlackChannelFlow ? { 'slack-channel': SlackChannelFlow } : {}),
  ...(TelegramFlow ? { 'telegram-private': TelegramFlow } : {}),
}

const TYPE_WORD: Record<NotificationChannel['type'], string> = { email: 'Email', slack: 'Slack', telegram: 'Telegram' }

/** An Add channel option's word: the type alone for its default kind, the kind after it otherwise. */
function optionLabel(option: NotificationAddOption): string {
  const plain = option.kind === 'address' || option.kind === 'private'
  if (plain) return TYPE_WORD[option.type]
  return `${TYPE_WORD[option.type]} ${option.kind === 'dm' ? 'DM' : option.kind}`
}

/** A channel that cannot deliver: its chip, and the fix on hover. */
const STATUS: Record<Exclude<NotificationChannel['status'], 'active'>, { label: string; tone: Tone; title: string }> = {
  unverified: { label: 'Unverified', tone: 'warning', title: 'Not confirmed yet. Use the emailed link or code.' },
  unlinked: { label: 'Not linked', tone: 'warning', title: 'The chat is unlinked. Add Telegram again.' },
  needs_reconnect: { label: 'Reconnect Slack', tone: 'error', title: "This org's Slack isn't connected." },
  workspace_mismatch: { label: 'Wrong workspace', tone: 'error', title: "Linked in another Slack workspace. Reconnect." },
}

const LEVEL_OPTIONS: SegmentedOption<NotificationLevel>[] = [
  { value: 'off', label: 'Off', help: 'Sends nothing.' },
  { value: 'brief', label: 'Brief', help: 'Sends a headline and a link.' },
  { value: 'full', label: 'Full', help: 'Sends the whole message.' },
]

type Change = { group: string; level: NotificationLevel } | { event: string; level: NotificationLevel }

/** The document with one change applied as the server will (`notification_catalog.apply_choice`): a group sets
 *  every event in it and drops their event choices. */
function applyChange(doc: NotificationChannelsDocument, channelId: string, change: Change): NotificationChannelsDocument {
  const channels = doc.channels.map(channel => {
    if (channel.id !== channelId) return channel
    const effective = { ...channel.effective }
    const groups = { ...channel.settings.groups }
    const events = { ...channel.settings.events }
    if ('group' in change) {
      groups[change.group] = change.level
      for (const event of doc.catalog.groups.find(g => g.key === change.group)?.events ?? []) {
        effective[event.key] = change.level
        delete events[event.key]
      }
    } else {
      effective[change.event] = change.level
      events[change.event] = change.level
    }
    return { ...channel, effective, settings: { groups, events } }
  })
  return { ...doc, channels }
}

/** The level a group's events share, or `mixed`. */
function groupLevel(group: NotificationCatalog['groups'][number], effective: NotificationChannel['effective']): NotificationLevel | 'mixed' {
  const levels = new Set(group.events.map(event => effective[event.key] ?? 'off'))
  return levels.size === 1 ? [...levels][0] : 'mixed'
}

/* ── Look ──
 * Rows divided by hairlines, as the console's lists. A level row wraps: its label takes its own line above the
 * switch where both do not fit (a phone). The left slot holds a group's chevron (or its blank, so labels align);
 * a tap target wide at phone width. */
const ROW_SX = { borderTop: 1, borderColor: 'divider' } as const
const LEVEL_ROW_SX = {
  display: 'flex',
  flexWrap: 'wrap',
  alignItems: 'center',
  columnGap: 1,
  rowGap: 0.5,
  minHeight: SIZE.tapTarget,
  py: 0.5,
  borderTop: 1,
  borderColor: 'divider',
} as const
const SLOT_SX = { width: grid(4), flexShrink: 0, display: 'flex', justifyContent: 'center', [PHONE]: { width: SIZE.tapTarget } } as const
const LEVEL_LABEL_SX = { display: 'flex', alignItems: 'center', flex: `1 1 ${grid(24)}`, minWidth: 0 } as const
const SWITCH_SX = { ml: 'auto' } as const
/* Each Off/Brief/Full segment a full tap target on a phone: tall is not enough, "Off" alone is narrower than 44px. */
const SEGMENT_SX = [tapTargetSx, { [PHONE]: { minWidth: SIZE.tapTarget } }]
/* A group's label as its toggle: left-aligned text, a tap target tall on a phone. */
const GROUP_TOGGLE_SX = {
  textAlign: 'left', alignSelf: 'stretch', borderRadius: RADIUS.sm,
  '&:focus-visible': { outline: 2, outlineColor: 'primary.main' },
  [PHONE]: { minHeight: SIZE.tapTarget },
} as const
const chevronSx = (open: boolean) => ({ transition: 'transform 140ms', transform: open ? 'rotate(90deg)' : 'none' })

// Default export only: one import spelling for one component.
function NotificationsPanel({ userId, readOnly = false }: {
  /** Another membership's panel (`/users/:id`); absent, the signed-in person's. */
  userId?: string
  /** Viewing without `users:edit`: every switch locked, no Remove. */
  readOnly?: boolean
}): JSX.Element {
  const base = userId ? `/api/users/${encodeURIComponent(userId)}/notification-channels` : '/api/me/notification-channels'
  const queryKey = ['notification-channels', userId ?? 'me']
  const queryClient = useQueryClient()
  const query = useQuery({ queryKey, queryFn: () => get<NotificationChannelsDocument>(base) })
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [flow, setFlow] = useState<FlowKey | null>(null)
  const [addingEmail, setAddingEmail] = useState(false)
  const { admin } = useAuth()
  const [open, setOpen] = useState<Record<string, boolean>>({})
  const [confirm, confirmDialog] = useConfirm()
  /* Only the newest write's answer is adopted: an older one landing late would undo a newer click. */
  const writes = useRef(0)

  const reload = () => void queryClient.invalidateQueries({ queryKey })

  async function save(channel: NotificationChannel, change: Change) {
    /* A refetch already in flight would land after the optimistic write and put the old level back. */
    await queryClient.cancelQueries({ queryKey })
    const before = queryClient.getQueryData<NotificationChannelsDocument>(queryKey)
    if (!before) return
    const seq = ++writes.current
    queryClient.setQueryData(queryKey, applyChange(before, channel.id, change))
    setError('')
    try {
      const doc = await patch<NotificationChannelsDocument>(`${base}/${channel.id}`, change)
      if (seq === writes.current) queryClient.setQueryData(queryKey, doc)
    } catch (err) {
      if (seq === writes.current) queryClient.setQueryData(queryKey, before)
      else reload()
      setError(apiDetail(err, `Could not save ${channel.label}'s alerts.`))
    }
  }

  async function remove(channel: NotificationChannel) {
    let doc: NotificationChannelsDocument | null = null
    const removed = await confirm(danger('Remove', {
      title: 'Remove channel?',
      message: `Alerts stop going to ${channel.label}.`,
      run: {
        action: async () => { doc = await del<NotificationChannelsDocument>(`${base}/${channel.id}`) },
        failed: 'Could not remove.',
      },
    }))
    if (removed && doc) queryClient.setQueryData(queryKey, doc)
  }

  function endFlow(problem?: string) {
    setFlow(null)
    if (problem) setNotice(problem)
  }

  if (query.isPending) return <LoadingState rows={3} label="Loading alerts" />
  if (query.isError) {
    return <LoadFailedState title="Alerts did not load." error={apiDetail(query.error, '')} onRetry={() => void query.refetch()} />
  }

  const { channels, catalog, add_options: addOptions } = query.data
  const listed = addOptions.filter(option =>
    (option.available && (FLOWS[flowKey(option)] || option.type === 'email')) || (!option.available && option.reason))
  const addItems: OverflowItem[] = listed.map(option => ({
    key: `${option.type}-${option.kind}`,
    label: optionLabel(option),
    ariaLabel: `Add ${optionLabel(option)}`,
    testId: `notif-add-${option.type}-${option.kind}`,
    disabled: !option.available,
    disabledReason: option.reason ?? undefined,
    onClick: () => {
      setNotice('')
      if (option.type === 'email') setAddingEmail(true)
      else setFlow(flowKey(option))
    },
  }))
  const Flow = flow ? FLOWS[flow] : undefined
  /* May add and verify email channels here: the email option is offered and open. */
  const mayProve = addOptions.some(option => option.type === 'email' && option.kind === 'address' && option.available)
  const emailPrefill = !userId && !channels.some(channel => channel.account_email) ? admin?.email ?? '' : ''

  const addButton = addItems.length > 0 ? (
    <OverflowMenu
      items={addItems}
      busy={flow !== null}
      button={{ label: 'Add channel', variant: 'outlined' }}
      sx={ROW_ACTION_SX}
      testId="notif-add-channel"
    />
  ) : (
    <DisabledReason reason="Nothing to add">
      <Button variant="outlined" disabled sx={ROW_ACTION_SX} data-testid="notif-add-channel">Add channel</Button>
    </DisabledReason>
  )

  return (
    <DetailSection title="Channels" actions={addButton} testId="notif-panel">
      {error && <Banner severity="error" onDismiss={() => setError('')}>{error}</Banner>}
      {notice && <Banner severity="warning" onDismiss={() => setNotice('')}>{notice}</Banner>}
      {channels.length === 0 ? (
        <EmptyState inline title="No channels. Nothing is sent." />
      ) : (
        <Box role="list" aria-label="Channels" sx={{ borderBottom: 1, borderColor: 'divider' }}>
          {channels.map(channel => (
            <ChannelRow
              key={channel.id}
              channel={channel}
              catalog={catalog}
              open={open[channel.id] ?? channels.length === 1}
              onToggle={() => setOpen(prev => ({ ...prev, [channel.id]: !(prev[channel.id] ?? channels.length === 1) }))}
              readOnly={readOnly}
              onChange={change => void save(channel, change)}
              onRemove={() => void remove(channel)}
              verifyBase={mayProve && !readOnly && channel.type === 'email' && channel.status === 'unverified' ? base : undefined}
              onVerified={reload}
              onReconnect={!userId && SlackFlow && channel.slack_kind === 'dm' && channel.status === 'workspace_mismatch'
                ? () => { setNotice(''); setFlow('slack-dm') }
                : undefined}
            />
          ))}
        </Box>
      )}
      {Flow && <Flow onLinked={reload} onEnd={endFlow} />}
      {addingEmail && (
        <AddEmailDialog base={base} prefill={emailPrefill} onClose={() => { setAddingEmail(false); reload() }}
          onAdded={problem => { setAddingEmail(false); reload(); if (problem) setNotice(problem) }} />
      )}
      {confirmDialog}
    </DetailSection>
  )
}

/** One destination: its line (a disclosure for its alert settings) and, open, the settings under it. */
function ChannelRow({ channel, catalog, open, onToggle, readOnly, onChange, onRemove, onReconnect, verifyBase, onVerified }: {
  channel: NotificationChannel
  catalog: NotificationCatalog
  open: boolean
  onToggle: () => void
  readOnly: boolean
  onChange: (change: Change) => void
  onRemove: () => void
  onReconnect?: () => void
  /** Set: an unverified email this viewer may prove — its channels' base path. */
  verifyBase?: string
  onVerified: () => void
}): JSX.Element {
  const settingsId = useId()
  const status = channel.status === 'active' ? null : STATUS[channel.status]
  const items: OverflowItem[] = readOnly ? [] : [
    ...(onReconnect ? [{ key: 'reconnect', label: 'Reconnect', ariaLabel: `Reconnect ${channel.label}`, onClick: onReconnect }] : []),
    { key: 'remove', label: 'Remove', ariaLabel: `Remove ${channel.label}`, danger: true, testId: 'notif-channel-remove', onClick: onRemove },
  ]
  return (
    <Box role="listitem" sx={ROW_SX} data-testid={`notif-channel-${channel.id}`} data-type={channel.type}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
        <Box
          component="button"
          type="button"
          aria-expanded={open}
          aria-controls={settingsId}
          onClick={onToggle}
          sx={[UNSTYLED_BUTTON_SX, {
            flex: '1 1 auto', minWidth: 0, display: 'flex', alignItems: 'center', gap: 1, textAlign: 'left',
            minHeight: SIZE.tapTarget, py: 0.5, borderRadius: RADIUS.sm, '&:focus-visible': { outline: 2, outlineColor: 'primary.main' },
          }]}
        >
          <Box sx={SLOT_SX}><ChevronRightIcon size={SIZE.iconSm} sx={chevronSx(open)} /></Box>
          <Typography variant="body2" component="span" sx={{ width: { xs: grid(11), sm: grid(12) }, flexShrink: 0, color: 'text.secondary' }}>
            {TYPE_WORD[channel.type]}
          </Typography>
          <Box component="span" sx={{ flex: '1 1 0', minWidth: 0, display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 1, rowGap: 0.5 }}>
            <Typography variant="body2" component="span" sx={[anywhereSx, { fontWeight: FONT_WEIGHT.medium }]}>{channel.label}</Typography>
            {status && <StatusChip label={status.label} tone={status.tone} title={status.title} wrap testId="notif-channel-status" />}
          </Box>
          {/* What a press does, in words: a lone channel opens by itself, so a bare chevron's first press closed it. */}
          <Typography variant="caption" component="span" aria-hidden="true" data-testid="notif-channel-toggle-word"
            sx={{ flexShrink: 0, color: 'primary.main' }}>
            {open ? 'Hide' : 'Show'}
          </Typography>
        </Box>
        <OverflowMenu items={items} ariaLabel={`Actions for ${channel.label}`} sx={tapTargetSx} testId="notif-channel-actions" />
      </Box>
      {verifyBase && <VerifyStrip channel={channel} base={verifyBase} onVerified={onVerified} />}
      {open && (
        <Box id={settingsId} role="group" aria-label={`${channel.label} alerts`} sx={{ pb: 1 }}>
          {(channel.type === 'slack' || channel.type === 'telegram') && (
            <Box sx={{ pb: 1, '&:empty': { display: 'none' } }}><NonBaaWarning surface={channel.type} /></Box>
          )}
          {catalog.groups.filter(group => group.events.length > 0).map(group => (
            <GroupRows key={group.key} group={group} channel={channel} readOnly={readOnly} onChange={onChange} />
          ))}
        </Box>
      )}
    </Box>
  )
}

/** Add ▸ Email: one address; the server mails it a link and a code, and the row shows Unverified until proven. */
function AddEmailDialog({ base, prefill, onClose, onAdded }: {
  base: string
  prefill: string
  onClose: () => void
  /** `problem`: added, but its confirm email was not sent (the row's Resend tries again). */
  onAdded: (problem?: string) => void
}): JSX.Element {
  const [address, setAddress] = useState(prefill)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function submit() {
    setBusy(true)
    setError('')
    try {
      await post<NotificationChannel>(base, { type: 'email', address: address.trim() })
      onAdded()
    } catch (err) {
      /* 503: the channel was added but its email did not go out; the row's Resend tries again. */
      if (err instanceof ApiError && err.status === 503) onAdded(apiDetail(err, 'The email was not sent. Use Resend.'))
      else {
        setError(apiDetail(err, 'Could not add the email.'))
        setBusy(false)
      }
    }
  }

  return (
    <FormDialog
      title="Add email"
      submitLabel="Add"
      busyLabel="Adding…"
      busy={busy}
      error={error}
      submitDisabled={!address.trim()}
      onSubmit={() => void submit()}
      onClose={onClose}
      testId="notif-add-email"
    >
      <FormTextField
        label="Email"
        type="email"
        value={address}
        onChange={setAddress}
        autoFocus
        autoComplete="off"
        hint="We'll email a link and a code to confirm it."
        testId="notif-add-email-address"
      />
    </FormDialog>
  )
}

/** How long Resend's "Sent." stays. */
const SENT_NOTE_MS = 4000
/* Under the row's line, aligned with its type word; wraps so the field and buttons stack on a phone. */
const STRIP_SX = { display: 'flex', flexDirection: 'column', gap: 0.5, pb: 1, pl: { xs: 0, sm: grid(4) } } as const
const STRIP_ROW_SX = { display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 1, rowGap: 0.5 } as const
const CODE_FIELD_SX = { flex: `1 1 ${grid(16)}`, maxWidth: { sm: grid(24) } } as const

/** An unverified email's proof: Verify (the account email, first, alone), then the emailed code, Confirm and Resend. */
function VerifyStrip({ channel, base, onVerified }: {
  channel: NotificationChannel
  base: string
  onVerified: () => void
}): JSX.Element {
  const path = `${base}/${channel.id}`
  /* A typed address was mailed when added; the account email is mailed only on Verify. */
  const [sent, setSent] = useState(!channel.account_email)
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState<'verify' | 'confirm' | 'resend' | null>(null)
  const [error, setError] = useState('')
  const [note, setNote] = useState('')

  useEffect(() => {
    if (!note) return
    const timer = setTimeout(() => setNote(''), SENT_NOTE_MS)
    return () => clearTimeout(timer)
  }, [note])

  async function resend(kind: 'verify' | 'resend') {
    setBusy(kind)
    setError('')
    setNote('')
    try {
      await post(`${path}/resend`, {})
      if (kind === 'verify') setSent(true)
      else setNote('Sent.')
    } catch (err) {
      setError(apiDetail(err, 'Could not send the email.'))
    } finally {
      setBusy(null)
    }
  }

  async function confirm() {
    if (!code.trim() || busy) return
    setBusy('confirm')
    setError('')
    setNote('')
    try {
      await post<NotificationChannel>(`${path}/verify`, { code: code.trim() })
      onVerified()
    } catch (err) {
      setError(apiDetail(err, 'Could not confirm.'))
    } finally {
      setBusy(null)
    }
  }

  /* The code field always shows (a code mailed before a reload still works); the account email's send reads
   * Verify until it has mailed one here, then Resend. */
  const sendKind = sent ? 'resend' : 'verify'
  return (
    <Box sx={STRIP_SX} data-testid="notif-verify">
      <Typography variant="body2" sx={{ color: 'text.secondary' }}>
        {sent ? 'Check your inbox for a link or a 6-digit code.' : 'Confirm this address to get alerts here.'}
      </Typography>
      {/* Not a <form>: the strip sits inside pages that are forms (Profile), and a nested form is invalid HTML whose
        * Enter would submit the page's form. Enter in the code field confirms instead. */}
      <Box role="group" aria-label={`Confirm ${channel.label}`} sx={STRIP_ROW_SX}>
        <FormTextField
          label="Code"
          value={code}
          onChange={setCode}
          autoComplete="one-time-code"
          size="small"
          sx={CODE_FIELD_SX}
          slotProps={{ htmlInput: {
            inputMode: 'numeric', maxLength: 6, 'aria-label': `Code for ${channel.label}`,
            onKeyDown: (e: KeyboardEvent<HTMLInputElement>) => {
              if (e.key !== 'Enter') return
              e.preventDefault()
              void confirm()
            },
          } }}
          testId="notif-verify-code"
        />
        {/* Before a code is mailed or typed the strip's one job is Verify: Confirm waits for a mail or a code
          * instead of standing greyed beside it. */}
        {(sent || code.trim()) && (
          <BusyButton type="button" onClick={() => void confirm()} variant="contained" busy={busy === 'confirm'} disabled={busy !== null || !code.trim()} sx={tapTargetSx}>
            Confirm
          </BusyButton>
        )}
        <BusyButton type="button" variant={sent ? 'text' : 'outlined'} busy={busy === sendKind} disabled={busy !== null}
          onClick={() => void resend(sendKind)} aria-label={sent ? undefined : `Verify ${channel.label}`} sx={tapTargetSx}>
          {sent ? 'Resend' : 'Verify'}
        </BusyButton>
      </Box>
      {error && <Typography variant="body2" role="alert" sx={[anywhereSx, { color: 'error.main' }]}>{error}</Typography>}
      {note && <Typography variant="body2" role="status" sx={{ color: 'text.secondary' }}>{note}</Typography>}
    </Box>
  )
}

/** A group's switch and, opened, one per event. A group of one alert has no chevron: its switch is the alert's. */
function GroupRows({ group, channel, readOnly, onChange }: {
  group: NotificationCatalog['groups'][number]
  channel: NotificationChannel
  readOnly: boolean
  onChange: (change: Change) => void
}): JSX.Element {
  const [open, setOpen] = useState(false)
  const labelId = useId()
  const eventsId = useId()
  const level = groupLevel(group, channel.effective)
  const expandable = group.events.length > 1
  return (
    <Box data-testid={`notif-group-${group.key}`}>
      <Box sx={LEVEL_ROW_SX}>
        {expandable ? (
          /* The whole label opens the single alerts, as a channel row does; the name starts with the visible words, so
             voice control reaches it by what it shows. */
          <Box
            component="button"
            type="button"
            aria-label={`${group.label}: each alert`}
            aria-expanded={open}
            aria-controls={eventsId}
            onClick={() => setOpen(prev => !prev)}
            data-testid="notif-group-toggle"
            sx={[UNSTYLED_BUTTON_SX, LEVEL_LABEL_SX, GROUP_TOGGLE_SX]}
          >
            <Box sx={SLOT_SX}><ChevronRightIcon size={SIZE.iconSm} sx={chevronSx(open)} /></Box>
            <Typography id={labelId} variant="body2" component="span" sx={anywhereSx}>{group.label}</Typography>
          </Box>
        ) : (
          <Box sx={LEVEL_LABEL_SX}>
            <Box sx={SLOT_SX} />
            <Typography id={labelId} variant="body2" sx={anywhereSx}>{group.label}</Typography>
          </Box>
        )}
        <SegmentedControl<NotificationLevel | 'mixed'>
          value={level}
          options={LEVEL_OPTIONS}
          ariaLabelledBy={labelId}
          testId={`notif-group-switch-${group.key}`}
          color="primary"
          disabled={readOnly}
          sx={SWITCH_SX}
          buttonSx={SEGMENT_SX}
          onChange={next => { if (next !== 'mixed') onChange({ group: group.key, level: next }) }}
        />
      </Box>
      {expandable && open && (
        <Box id={eventsId} role="group" aria-labelledby={labelId}>
          {group.events.map(event => (
            <EventRow key={event.key} event={event} level={channel.effective[event.key] ?? 'off'} readOnly={readOnly}
              onChange={next => onChange({ event: event.key, level: next })} />
          ))}
        </Box>
      )}
    </Box>
  )
}

/** One alert's switch, indented under its group's label. */
function EventRow({ event, level, readOnly, onChange }: {
  event: { key: string; label: string }
  level: NotificationLevel
  readOnly: boolean
  onChange: (level: NotificationLevel) => void
}): JSX.Element {
  const labelId = useId()
  return (
    <Box sx={LEVEL_ROW_SX} data-testid={`notif-event-${event.key}`}>
      <Box sx={LEVEL_LABEL_SX}>
        <Box sx={SLOT_SX} />
        <Typography id={labelId} variant="body2" sx={[anywhereSx, { pl: 2, color: level === 'off' ? 'text.secondary' : 'text.primary' }]}>
          {event.label}
        </Typography>
      </Box>
      <SegmentedControl
        value={level}
        options={LEVEL_OPTIONS}
        ariaLabelledBy={labelId}
        testId={`notif-event-switch-${event.key}`}
        color="primary"
        disabled={readOnly}
        sx={SWITCH_SX}
        buttonSx={SEGMENT_SX}
        onChange={onChange}
      />
    </Box>
  )
}

export default NotificationsPanel
