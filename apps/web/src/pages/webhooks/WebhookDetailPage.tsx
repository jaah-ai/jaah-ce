import { useMemo, useRef, useState, type JSX, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import TableBody from '@mui/material/TableBody'
import TableCell from '@mui/material/TableCell'
import TableHead from '@mui/material/TableHead'
import TableRow from '@mui/material/TableRow'
import { apiDetail, retryUnlessNotFound } from '../../api'
import Breadcrumb from '../../components/Breadcrumb'
import { danger, useConfirm } from '../../components/ConfirmDialog'
import { Tabs, TabPanel } from '../../components/Tabs'
import { useLeaveGuard } from '../../hooks/useLeaveGuard'
import { useProjects } from '../../hooks/useRosters'
import { useSubmit } from '../../hooks/useSubmit'
import { useUrlTab } from '../../hooks/useUrlTab'
import { absoluteTime, compactAge } from '../../lib/relativeTime'
import { grid } from '../../theme'
import Banner from '../../ui/Banner'
import CancelSaveBar from '../../ui/CancelSaveBar'
import { DataTable } from '../../ui/DataTable'
import DetailPage, { DETAIL_SECTIONS_SX, DetailField, DetailSection } from '../../ui/detail/DetailPage'
import { detailStatus } from '../../ui/detail/DetailStatus'
import EmptyState from '../../ui/EmptyState'
import { EntityNameFields } from '../../ui/EntityNameFields'
import FormBox from '../../ui/FormBox'
import FormTextField from '../../ui/FormTextField'
import { PHONE, tapTargetSx } from '../../ui/listStyles'
import LoadFailedState from '../../ui/LoadFailedState'
import LoadingState from '../../ui/LoadingState'
import { OverflowMenu } from '../../ui/OverflowMenu'
import { LoadMoreFooter } from '../../ui/PagedList'
import SecretOnceDialog from '../../ui/SecretOnceDialog'
import SettingCheckbox from '../../ui/SettingCheckbox'
import { StatusChip } from '../../ui/StatusChip'
import Tip from '../../ui/Tip'
import { LeaveGuardDialog } from '../../ui/UnsavedChangesDialog'
import { refusalCode } from '../../lib/refusalCode'
import {
  createWebhook,
  deleteWebhook,
  deliveryState,
  EVENT_TYPES_KEY,
  getWebhook,
  listWebhookDeliveries,
  listWebhookEventTypes,
  redeliverWebhookDelivery,
  rotateWebhookSecret,
  testWebhook,
  updateWebhook,
  webhookKey,
  webhookPath,
  WEBHOOKS_KEY,
  WEBHOOKS_PATH,
  type Webhook,
  type WebhookBody,
  type WebhookDelivery,
} from './api'

/* ONE WEBHOOK ENDPOINT (`GET /api/webhooks/{id}`, `@org_admin`), opened from Webhooks; `/integrations/webhooks/new`
 * is the same page in create mode (its one tab, so no strip). A tabbed detail (ui-style-guide): Settings — name,
 * description, URL, enabled, the event types (`GET /api/webhooks/event-types`, the catalog's order) and the projects,
 * one form behind one Save (`PATCH`, only the changed keys); Secret — when it was set or rotated, and Rotate; Deliveries
 * — the delivery log, newest first, paged by the server's cursor, each row with Redeliver. The secret is answered ONCE,
 * by the create and by Rotate, and shown in `SecretOnceDialog`; nothing reads it again. A URL the server refuses (not
 * https, or a private, link-local or metadata address: `url_refused`) is the URL field's own error, nothing saved.
 * The header's Send test posts a `ping` now; ⋯ deletes, confirmed. */

const TABS = [
  { id: 'settings', label: 'Settings' },
  { id: 'secret', label: 'Secret' },
  { id: 'deliveries', label: 'Deliveries' },
] as const
type WebhookTab = (typeof TABS)[number]['id']

const FORM_ID = 'webhook-form'

/** A delivery column a phone drops, so Redeliver stays on screen without a sideways scroll. */
const WIDE_ONLY_SX = { [PHONE]: { display: 'none' } } as const

type Draft = WebhookBody

const EMPTY: Draft = { name: '', description: '', url: '', event_types: [], project_ids: null, enabled: true }

const draftOf = (hook: Webhook): Draft => ({
  name: hook.name,
  description: hook.description,
  url: hook.url,
  event_types: [...hook.event_types],
  project_ids: hook.project_ids === null ? null : [...hook.project_ids],
  enabled: hook.enabled,
})

const sameList = <T,>(a: readonly T[] | null, b: readonly T[] | null) =>
  a === null || b === null ? a === b : a.length === b.length && [...a].map(String).sort().join() === [...b].map(String).sort().join()

/** Only what changed: the PATCH keeps every absent key. */
function patchOf(saved: Draft, draft: Draft): Partial<WebhookBody> {
  const out: Partial<WebhookBody> = {}
  if (draft.name.trim() !== saved.name) out.name = draft.name.trim()
  if (draft.description.trim() !== saved.description) out.description = draft.description.trim()
  if (draft.url.trim() !== saved.url) out.url = draft.url.trim()
  if (!sameList(draft.event_types, saved.event_types)) out.event_types = draft.event_types
  if (!sameList(draft.project_ids, saved.project_ids)) out.project_ids = draft.project_ids
  if (draft.enabled !== saved.enabled) out.enabled = draft.enabled
  return out
}

/** Why Save cannot run, or null. */
function problemOf(draft: Draft): string | null {
  if (!draft.name.trim()) return 'Name is empty'
  if (!draft.url.trim()) return 'URL is empty'
  if (!/^https:\/\//i.test(draft.url.trim())) return 'URL must start with https://'
  if (draft.event_types.length === 0) return 'Pick at least one event'
  if (draft.project_ids !== null && draft.project_ids.length === 0) return 'Pick a project, or All projects'
  return null
}

export default function WebhookDetailPage({ mode }: { mode?: 'create' }): JSX.Element {
  const { id = '' } = useParams<{ id: string }>()
  const creating = mode === 'create'
  const read = useQuery({
    queryKey: webhookKey(id),
    queryFn: () => getWebhook(id),
    enabled: !creating,
    retry: retryUnlessNotFound,
    meta: { persist: false },
  })
  const navigate = useNavigate()
  /** The secret just answered by create or rotate: shown once, dropped when the dialog closes. Held here, above the
   *  keyed editor, so the remount a rotate's new `updated_at` causes cannot drop it before it is read. */
  const [reveal, setReveal] = useState<Reveal | null>(null)
  const hook = creating ? null : read.data
  const crumbs = (name: string) => <Breadcrumb items={[{ label: 'Webhooks', to: WEBHOOKS_PATH }, { label: name }]} />
  const closeReveal = () => {
    const made = reveal
    setReveal(null)
    // `replace`: Back lands on the list, never on a create form that would make a second endpoint.
    if (creating && made) navigate(webhookPath(made.id), { replace: true })
  }
  const dialog = reveal !== null && (
    <SecretOnceDialog
      secret={reveal.secret}
      title={`${reveal.name}: signing secret`}
      notice="Copy it now. You won’t see this secret again."
      submitLabel="Done"
      testIdPrefix="webhook-secret"
      valueTestId="webhook-secret-value"
      onClose={closeReveal}
    />
  )
  if (creating) {
    return <><WebhookEditor key="new" hook={null} crumbs={crumbs('New webhook')} onSecret={setReveal} />{dialog}</>
  }
  if (!hook) return detailStatus(read, crumbs('Webhook'), 'webhook')
  // A save remounts the editor at the saved endpoint.
  return <><WebhookEditor key={hook.updated_at ?? hook.id} hook={hook} crumbs={crumbs(hook.name)} onSecret={setReveal} />{dialog}</>
}

type Reveal = { id: string; name: string; secret: string }

function WebhookEditor({ hook, crumbs, onSecret }: {
  hook: Webhook | null
  crumbs: ReactNode
  onSecret: (reveal: Reveal) => void
}): JSX.Element {
  const client = useQueryClient()
  const creating = hook === null
  const tabs = creating ? TABS.slice(0, 1) : TABS
  const [tab, selectTab] = useUrlTab<WebhookTab>(tabs, 'settings')
  const saved = useMemo(() => (hook ? draftOf(hook) : EMPTY), [hook])
  const [draft, setDraft] = useState<Draft>(saved)
  const [urlError, setUrlError] = useState('')
  /** Create mode once the endpoint exists: the form is spent, and leaving it asks nothing. */
  const [created, setCreated] = useState(false)
  const { busy, formError, setFormError, run } = useSubmit()
  const actions = useSubmit()
  const [confirm, confirmDialog] = useConfirm()
  const changes = useMemo(() => patchOf(saved, draft), [saved, draft])
  const dirty = !created && (creating ? draft.name.trim() !== '' || draft.url.trim() !== '' || draft.event_types.length > 0 : Object.keys(changes).length > 0)
  const problem = problemOf(draft)

  const edit = (update: (d: Draft) => Draft) => {
    setDraft(update)
    setFormError('')
  }

  // A refused URL is the URL field's error; every other refusal is the banner's.
  const mapError = (err: unknown, fallback: string): string => {
    if (refusalCode(err) === 'url_refused') {
      setUrlError(apiDetail(err, 'This URL is refused'))
      return ''
    }
    return apiDetail(err, fallback)
  }

  const save = async (): Promise<boolean> => {
    if (!dirty || problem) return false
    setUrlError('')
    if (creating) {
      const landed = await run('Could not create the webhook. Retry.', () => createWebhook({
        ...draft, name: draft.name.trim(), description: draft.description.trim(), url: draft.url.trim(),
      }), { mapError })
      if (!landed) return false
      // The secret lives only in the reveal's state, never in the query cache.
      const { secret, ...made } = landed.value
      client.setQueryData(webhookKey(made.id), made)
      void client.invalidateQueries({ queryKey: WEBHOOKS_KEY })
      // The page stays put under the reveal; closing it moves to the endpoint's own page.
      setCreated(true)
      onSecret({ id: made.id, name: made.name, secret })
      return true
    }
    const landed = await run('Could not save. Retry.', () => updateWebhook(hook.id, changes), { mapError })
    if (!landed) return false
    client.setQueryData(webhookKey(hook.id), landed.value)
    void client.invalidateQueries({ queryKey: WEBHOOKS_KEY })
    return true
  }
  const guard = useLeaveGuard(dirty, save, ['tab'])

  const remove = async () => {
    if (!hook) return
    const yes = await confirm(danger('Delete', {
      title: 'Delete webhook?',
      message: 'Its delivery log goes too. Nothing more is sent to this URL.',
      identity: hook.name,
      run: { action: () => deleteWebhook(hook.id), failed: 'Could not delete. Retry.' },
    }))
    if (!yes) return
    void client.invalidateQueries({ queryKey: WEBHOOKS_KEY })
    client.removeQueries({ queryKey: webhookKey(hook.id) })
    guard.leaveNow(WEBHOOKS_PATH)
  }

  const rotate = async () => {
    if (!hook) return
    const yes = await confirm({
      title: 'Rotate secret?',
      message: 'A new secret signs every delivery. The old one also signs for 24 hours.',
      identity: hook.name,
      confirmLabel: 'Rotate',
    })
    if (!yes) return
    const landed = await actions.run('Could not rotate the secret. Retry.', () => rotateWebhookSecret(hook.id))
    if (!landed) return
    const { secret, ...row } = landed.value
    onSecret({ id: row.id, name: row.name, secret })
    client.setQueryData(webhookKey(hook.id), row)
  }

  const sendTest = async () => {
    if (!hook) return
    const landed = await actions.run('Could not send the test. Retry.', () => testWebhook(hook.id))
    if (!landed) return
    // Not the endpoint's own read: a refetch carrying another admin's save would remount this form mid-edit.
    void client.invalidateQueries({ queryKey: ['webhook-deliveries', hook.id] })
    void client.invalidateQueries({ queryKey: WEBHOOKS_KEY })
    selectTab('deliveries')
  }

  return (
    <DetailPage
      testId="webhook-detail"
      breadcrumb={crumbs}
      title={hook?.name ?? 'New webhook'}
      titleAside={hook && (
        <StatusChip label={hook.enabled ? 'Enabled' : 'Disabled'} tone={hook.enabled ? 'success' : 'neutral'} data-testid="webhook-status" />
      )}
      actions={hook && (
        <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1 }}>
          <Button variant="outlined" onClick={() => void sendTest()} disabled={actions.busy} sx={tapTargetSx} data-testid="webhook-test">
            Send test
          </Button>
          <OverflowMenu
            ariaLabel={`${hook.name} actions`}
            testId="webhook-actions"
            items={[{ key: 'delete', label: 'Delete', ariaLabel: `Delete ${hook.name}`, danger: true, testId: 'webhook-delete', onClick: () => void remove() }]}
          />
        </Box>
      )}
      actionBar={tab === 'settings' && (
        <CancelSaveBar
          testId="webhook-form-actions"
          saveTestId="webhook-save"
          discardTestId="webhook-discard"
          creating={creating}
          cancelTo={creating ? WEBHOOKS_PATH : undefined}
          dirty={dirty}
          busy={busy}
          disabled={problem !== null || created}
          saveReason={dirty && problem ? problem : undefined}
          onDiscard={creating ? undefined : () => { setDraft(saved); setUrlError(''); setFormError('') }}
          saveProps={{ form: FORM_ID }}
        />
      )}
    >
      {formError && <Banner severity="error" testId="webhook-form-error">{formError}</Banner>}
      {actions.formError && <Banner severity="error" testId="webhook-action-error">{actions.formError}</Banner>}
      {!creating && <Tabs tabs={tabs} tab={tab} onSelect={selectTab} label="Webhook sections" idPrefix="webhook" />}
      <TabPanel id="settings" current={tab} idPrefix="webhook">
        <FormBox id={FORM_ID} onSubmit={save}>
          <SettingsTab draft={draft} edit={edit} urlError={urlError} clearUrlError={() => setUrlError('')} disabled={busy || created} autoFocus={creating} />
        </FormBox>
      </TabPanel>
      {hook && (
        <>
          <TabPanel id="secret" current={tab} idPrefix="webhook">
            <Box sx={DETAIL_SECTIONS_SX}>
              <DetailSection
                fields
                testId="webhook-secret"
                actions={
                  <Button type="button" variant="outlined" size="small" disabled={actions.busy} sx={tapTargetSx}
                    onClick={() => void rotate()} data-testid="webhook-rotate">
                    Rotate
                  </Button>
                }
              >
                <DetailField label="Secret">{hook.secret_set ? 'Set' : 'Not set'}</DetailField>
                <DetailField label="Created">{absoluteTime(hook.created_at)}{hook.created_by ? ` by ${hook.created_by}` : ''}</DetailField>
                <DetailField label="Rotated">{hook.secret_rotated_at ? absoluteTime(hook.secret_rotated_at) : 'Never'}</DetailField>
              </DetailSection>
            </Box>
          </TabPanel>
          <TabPanel id="deliveries" current={tab} idPrefix="webhook">
            {tab === 'deliveries' && <DeliveriesTab id={hook.id} />}
          </TabPanel>
        </>
      )}
      {confirmDialog}
      <LeaveGuardDialog leave={guard} message="This webhook has unsaved changes. Save them before leaving?" />
    </DetailPage>
  )
}

function SettingsTab({ draft, edit, urlError, clearUrlError, disabled, autoFocus }: {
  draft: Draft
  edit: (update: (d: Draft) => Draft) => void
  urlError: string
  clearUrlError: () => void
  disabled: boolean
  autoFocus: boolean
}): JSX.Element {
  const types = useQuery({ queryKey: EVENT_TYPES_KEY, queryFn: listWebhookEventTypes, staleTime: 5 * 60_000 })
  const projects = useProjects({ staleTime: 60_000 })
  const projectRows = [...(projects.data ?? [])].sort((a, b) => a.name.localeCompare(b.name))
  const toggle = <T,>(list: readonly T[], value: T, on: boolean): T[] =>
    on ? [...list.filter(v => v !== value), value] : list.filter(v => v !== value)
  // An event type the endpoint holds that the catalog no longer lists stays visible, so it can be unticked.
  const known = types.data ?? []
  const stray = draft.event_types.filter(t => !known.some(k => k.type === t))

  return (
    <Box sx={DETAIL_SECTIONS_SX}>
      <DetailSection title="Endpoint">
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, maxWidth: grid(80) }}>
          <EntityNameFields
            idPrefix="webhook"
            disabled={disabled}
            name={{ value: draft.name, onChange: v => edit(d => ({ ...d, name: v })), autoFocus, testId: 'webhook-name', maxLength: 200 }}
            description={{ value: draft.description, onChange: v => edit(d => ({ ...d, description: v })), maxLength: 2000, testId: 'webhook-description' }}
          />
          <FormTextField
            label="URL"
            value={draft.url}
            required
            disabled={disabled}
            placeholder="https://example.com/jaah-events"
            help={{ tip: 'Jaah POSTs each event here. https only; private addresses are refused.', docs: 'using-jaah/webhooks/receive-events-with-webhooks' }}
            error={urlError || undefined}
            onChange={v => {
              clearUrlError()
              edit(d => ({ ...d, url: v }))
            }}
            testId="webhook-url"
          />
          <SettingCheckbox
            name="enabled"
            label="Enabled"
            help="Off: nothing is sent. On again: sends what happens next, not what was missed."
            checked={draft.enabled}
            disabled={disabled}
            onChange={checked => edit(d => ({ ...d, enabled: checked }))}
            testId="webhook-enabled"
          />
        </Box>
      </DetailSection>
      <DetailSection title="Events" testId="webhook-events">
        {types.isError && <Banner severity="error">{apiDetail(types.error, 'Could not load the event types')}</Banner>}
        {types.isPending ? <LoadingState rows={3} /> : (
          <Box sx={{ display: 'flex', flexDirection: 'column' }}>
            {known.map(t => (
              <SettingCheckbox
                key={t.type}
                name={`event-${t.type}`}
                label={t.label}
                help={t.description}
                checked={draft.event_types.includes(t.type)}
                disabled={disabled}
                onChange={checked => edit(d => ({ ...d, event_types: toggle(d.event_types, t.type, checked) }))}
                testId={`webhook-event-${t.type}`}
              />
            ))}
            {stray.map(t => (
              <SettingCheckbox
                key={t}
                name={`event-${t}`}
                label={t}
                checked
                disabled={disabled}
                onChange={() => edit(d => ({ ...d, event_types: d.event_types.filter(v => v !== t) }))}
                testId={`webhook-event-${t}`}
              />
            ))}
          </Box>
        )}
      </DetailSection>
      <DetailSection title="Projects" testId="webhook-projects">
        <Box sx={{ display: 'flex', flexDirection: 'column' }}>
          <SettingCheckbox
            name="all-projects"
            label="All projects"
            help="Also sends events of projects added later."
            checked={draft.project_ids === null}
            disabled={disabled}
            onChange={checked => edit(d => ({ ...d, project_ids: checked ? null : [] }))}
            testId="webhook-all-projects"
          />
          {draft.project_ids !== null && (
            projects.isPending ? <LoadingState rows={2} />
              : projectRows.length === 0 ? <EmptyState inline title="No projects." />
                : projectRows.map(p => (
                  <SettingCheckbox
                    key={p.id}
                    name={`project-${p.id}`}
                    label={p.name}
                    checked={draft.project_ids?.includes(p.id) ?? false}
                    disabled={disabled}
                    onChange={checked => edit(d => ({ ...d, project_ids: toggle(d.project_ids ?? [], p.id, checked) }))}
                    testId={`webhook-project-${p.id}`}
                  />
                ))
          )}
        </Box>
      </DetailSection>
    </Box>
  )
}

function DeliveriesTab({ id }: { id: string }): JSX.Element {
  const client = useQueryClient()
  const moreRef = useRef(false)
  const first = useQuery({
    queryKey: ['webhook-deliveries', id],
    queryFn: () => listWebhookDeliveries(id, null),
    // Paused once older pages are loaded: a refresh would push rows off page one past what `more` holds.
    refetchInterval: (query) => (query.state.data && moreRef.current ? false : 15_000),
  })
  const [more, setMore] = useState<WebhookDelivery[]>([])
  const [cursor, setCursor] = useState<string | null | undefined>(undefined)
  const actions = useSubmit()
  const paging = useSubmit()
  const [busyId, setBusyId] = useState<string | null>(null)

  if (first.isError && !first.data) {
    return <LoadFailedState title="Deliveries unavailable" error={apiDetail(first.error, 'Could not load deliveries')} onRetry={() => void first.refetch()} />
  }
  if (!first.data) return <LoadingState rows={4} />
  const next = cursor === undefined ? first.data.next_cursor : cursor
  const rows = [...first.data.items, ...more.filter(m => !first.data.items.some(f => f.id === m.id))]
  if (rows.length === 0) return <EmptyState title="Nothing sent yet" />

  const loadMore = async () => {
    if (!next) return
    const landed = await paging.run('Could not load more. Retry.', () => listWebhookDeliveries(id, next))
    if (!landed) return
    moreRef.current = true
    setMore(m => [...m, ...landed.value.items])
    setCursor(landed.value.next_cursor)
  }

  const redeliver = async (delivery: WebhookDelivery) => {
    setBusyId(delivery.id)
    const landed = await actions.run('Could not redeliver. Retry.', () => redeliverWebhookDelivery(id, delivery.id))
    setBusyId(null)
    if (!landed) return
    setMore(m => m.map(r => (r.id === landed.value.id ? landed.value : r)))
    void client.invalidateQueries({ queryKey: ['webhook-deliveries', id] })
    void client.invalidateQueries({ queryKey: WEBHOOKS_KEY })
  }

  return (
    <Box sx={DETAIL_SECTIONS_SX}>
      {actions.formError && <Banner severity="error" testId="webhook-delivery-error">{actions.formError}</Banner>}
      {paging.formError && <Banner severity="error" testId="webhook-deliveries-more-error">{paging.formError}</Banner>}
      <DataTable ariaLabel="Deliveries" testId="webhook-deliveries">
        <TableHead>
          <TableRow>
            <TableCell>When</TableCell>
            <TableCell>Event</TableCell>
            <TableCell>Status</TableCell>
            <TableCell align="right">Code</TableCell>
            <TableCell align="right" sx={WIDE_ONLY_SX}>Latency</TableCell>
            <TableCell align="right" sx={WIDE_ONLY_SX}>Tries</TableCell>
            <TableCell padding="none" />
          </TableRow>
        </TableHead>
        <TableBody>
          {rows.map(d => {
            const state = deliveryState(d)
            const why = [d.last_error, d.state === 'pending' && d.next_attempt_at ? `Next try ${absoluteTime(d.next_attempt_at)}` : null]
              .filter(Boolean).join(' · ')
            const chip = <StatusChip label={state.label} tone={state.tone} data-state={d.state} />
            return (
              <TableRow key={d.id} data-testid="webhook-delivery-row" data-state={d.state}>
                <TableCell><Tip title={absoluteTime(d.created_at)}><span>{compactAge(d.created_at)}</span></Tip></TableCell>
                <TableCell>{d.event_type}</TableCell>
                <TableCell>{why ? <Tip title={why}><span>{chip}</span></Tip> : chip}</TableCell>
                <TableCell align="right" data-testid="webhook-delivery-code">{d.last_status_code ?? '—'}</TableCell>
                <TableCell align="right" sx={WIDE_ONLY_SX}>{d.last_latency_ms !== null ? `${d.last_latency_ms} ms` : '—'}</TableCell>
                <TableCell align="right" sx={WIDE_ONLY_SX}>{d.attempts}</TableCell>
                <TableCell padding="none" sx={{ textAlign: 'right' }}>
                  <Button type="button" size="small" variant="text" sx={tapTargetSx}
                    disabled={busyId !== null}
                    onClick={() => void redeliver(d)}
                    aria-label={`Redeliver ${d.event_type} delivery`}
                    data-testid="webhook-redeliver">
                    Redeliver
                  </Button>
                </TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </DataTable>
      <LoadMoreFooter hasMore={!!next} loading={paging.busy} onMore={() => void loadMore()} testId="webhook-deliveries-more" />
    </Box>
  )
}
