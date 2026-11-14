import { useMemo, useState, type JSX, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Link as RouterLink, useParams } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import IconButton from '@mui/material/IconButton'
import Link from '@mui/material/Link'
import TableBody from '@mui/material/TableBody'
import TableCell from '@mui/material/TableCell'
import TableHead from '@mui/material/TableHead'
import TableRow from '@mui/material/TableRow'
import { apiDetail, get, retryUnlessNotFound } from '../../api'
import Breadcrumb from '../../components/Breadcrumb'
import { danger, useConfirm } from '../../components/ConfirmDialog'
import { Tabs, TabPanel } from '../../components/Tabs'
import { useApiRead } from '../../hooks/useApiRead'
import { useLeaveGuard } from '../../hooks/useLeaveGuard'
import { PROJECTS_KEY, useProjects } from '../../hooks/useRosters'
import { useSubmit } from '../../hooks/useSubmit'
import { useUrlTab } from '../../hooks/useUrlTab'
import { readAsOnce, useActAsRows } from '../../lib/actAs'
import { absoluteTime, compactAge, relativeAge } from '../../lib/relativeTime'
import { grid } from '../../theme'
import Banner from '../../ui/Banner'
import CancelSaveBar from '../../ui/CancelSaveBar'
import CellLabel from '../../ui/CellLabel'
import { CopyableValue } from '../../ui/CopyableValue'
import { DataTable } from '../../ui/DataTable'
import { TaskLink } from '../../ui/DetailFields'
import DetailPage, { DETAIL_SECTIONS_SX, DetailField, DetailSection } from '../../ui/detail/DetailPage'
import { detailStatus } from '../../ui/detail/DetailStatus'
import EmptyState from '../../ui/EmptyState'
import ExternalLink from '../../ui/ExternalLink'
import FormBox from '../../ui/FormBox'
import FormTextField from '../../ui/FormTextField'
import { CloseIcon } from '../../ui/icons'
import { phoneCardSx, phoneColumnOrder, tapTargetSx, wordsSx } from '../../ui/listStyles'
import LoadFailedState from '../../ui/LoadFailedState'
import LoadingState from '../../ui/LoadingState'
import { ActingForNotice, OrgAside, useOtherOrg } from '../../ui/OrgIndicator'
import { OverflowMenu } from '../../ui/OverflowMenu'
import SecretOnceDialog from '../../ui/SecretOnceDialog'
import SelectField from '../../ui/SelectField'
import SettingCheckbox from '../../ui/SettingCheckbox'
import { StatusChip, type Tone } from '../../ui/StatusChip'
import Tip from '../../ui/Tip'
import { StartsTasksChip } from '../../ui/MarkChip'
import { LeaveGuardDialog } from '../../ui/UnsavedChangesDialog'
import type { OrgRef, Project } from '../../types'
import {
  authLabel,
  DEFAULT_START_LABEL,
  deleteTrackerConnection,
  eventPreview,
  getTrackerConnection,
  getTrackerWebhooks,
  listTrackerContainers,
  listTrackerEvents,
  rotateTrackerWebhook,
  trackerKey,
  containerNoun,
  withArticle,
  trackerLabel,
  TRACKERS_KEY,
  TRACKERS_PATH,
  updateTrackerConnection,
  type TrackerConnection,
  type TrackerEvent,
  type TrackerSettingsPatch,
} from './api'
import { takeAutomationOwnership } from '../automations/api'
import ContainerSelect from './ContainerSelect'
import { WebhookUrlField } from './WebhookUrlField'

/* ONE TRACKER CONNECTION (`GET /api/tracker-connections/{id}`, `@org_admin`), opened from Issue trackers. A tabbed
 * detail (ui-style-guide): Connection — what it is and its automation, read-only; Mapping — which tracker container
 * files into which Jaah project, the start label and the done state; People — whether tracker users who are no org
 * member may start tasks; Events — the newest events its trigger logged. Mapping and People are one form behind one
 * Save (`PATCH`, only the changed keys); leaving with unsaved edits asks first. The header's ⋯ disconnects, confirmed.
 * The container picker reads the tracker's own list; while that answers 409 (the tracker's adapter is not installed
 * yet) or fails (a 409 may carry the adapter's reason, e.g. no sign-in), the container is a typed id with the reason under it.
 * A connection that posts to its own URL (not Linear, not a Jira Forge app) takes a new webhook URL and secret from
 * Connection, confirmed (the old pair stops at once); the new pair shows once, as at connect. Connection also shows
 * when the last event arrived (the newest row Events lists) and what the tracker's webhooks post to (`GET …/webhooks`,
 * read live): a stale or missing one names its fix, and Backlog's carries the address to paste. A connection whose automation is owned from another
 * org (every start refused) says so, with Take ownership.
 * The title names the connection's org (operator only), and the Project picker offers only that org's projects. An
 * operator on another org's connection writes as that org (`lib/actAs.ts::useActAsRows`), and the page says so; its
 * Project picker reads that org's roster as that org (`readAsOnce`), so the operator's org filter cannot empty it. */

const TABS = [
  { id: 'connection', label: 'Connection' },
  { id: 'mapping', label: 'Mapping' },
  { id: 'people', label: 'People' },
  { id: 'events', label: 'Events' },
] as const
type TrackerTab = (typeof TABS)[number]['id']
const EDITABLE: readonly TrackerTab[] = ['mapping', 'people']

const FORM_ID = 'tracker-form'

/* An event's outcome chip: the server's word (`app/triggers.py::OUTCOME_LABELS`), toned by its `state`. Passed over by
   design (filtered, a duplicate, capped, skipped) is neutral, not a failure; a state this map does not know is too. */
const EVENT_TONE: Record<string, Tone> = {
  received: 'info', preflighting: 'info', queued: 'info', filing: 'info', held: 'info',
  dispatched: 'success', failed: 'error', unmatched: 'warning',
}

/* `editing`: the row has full option lists. A saved row carries only its picked value until it is focused or
   pressed, so the page's text (the hidden tabs included) and a screen reader's read of it are not every option. */
type MappingRow = { key: number; container_id: string; project_id: string; editing: boolean }
type Draft = { mappings: MappingRow[]; start_label: string; done_state: string; allow_unmapped_users: boolean }

let nextRowKey = 0
const rowKey = () => (nextRowKey += 1)

function draftOf(conn: TrackerConnection): Draft {
  const s = conn.settings
  return {
    mappings: (s?.mappings ?? []).map(m => ({ key: rowKey(), container_id: m.container_id, project_id: String(m.project_id), editing: false })),
    start_label: s?.start_label ?? DEFAULT_START_LABEL,
    done_state: s?.done_state ?? '',
    allow_unmapped_users: s?.allow_unmapped_users === true,
  }
}

/** Only what changed: the PATCH keeps every absent key. */
function patchOf(conn: TrackerConnection, draft: Draft): TrackerSettingsPatch {
  const saved = draftOf(conn)
  const out: TrackerSettingsPatch = {}
  const pairs = (rows: MappingRow[]) => JSON.stringify(rows.map(r => [r.container_id.trim(), r.project_id]))
  if (pairs(draft.mappings) !== pairs(saved.mappings)) {
    out.mappings = draft.mappings.map(r => ({ container_id: r.container_id.trim(), project_id: Number(r.project_id) }))
  }
  if (draft.start_label.trim() !== saved.start_label) out.start_label = draft.start_label.trim()
  if (draft.done_state.trim() !== saved.done_state) out.done_state = draft.done_state.trim() || null
  if (draft.allow_unmapped_users !== saved.allow_unmapped_users) out.allow_unmapped_users = draft.allow_unmapped_users
  return out
}

/** Why Save cannot run, or null. */
function problemOf(draft: Draft, noun: string): string | null {
  if (draft.mappings.some(r => !r.container_id.trim() || !r.project_id)) return `Pick ${withArticle(noun)} and a project on every row`
  const containers = draft.mappings.map(r => r.container_id.trim())
  if (new Set(containers).size !== containers.length) return `${withArticle(noun).replace(/^a/, 'A')} is mapped twice`
  if (!draft.start_label.trim()) return 'Start label is empty'
  return null
}

export default function TrackerDetailPage(): JSX.Element {
  const { id = '' } = useParams<{ id: string }>()
  const read = useQuery({
    queryKey: trackerKey(id),
    queryFn: () => getTrackerConnection(id),
    retry: retryUnlessNotFound,
    meta: { persist: false },
  })
  const conn = read.data
  const crumbs = <Breadcrumb items={[{ label: 'Issue trackers', to: TRACKERS_PATH }, { label: conn?.name ?? 'Tracker' }]} />
  if (!conn) return detailStatus(read, crumbs, 'tracker')
  // A save remounts the editor at the saved settings.
  return <TrackerEditor key={conn.updated_at ?? conn.id} conn={conn} crumbs={crumbs} />
}

function TrackerEditor({ conn, crumbs }: { conn: TrackerConnection; crumbs: ReactNode }): JSX.Element {
  const client = useQueryClient()
  const [tab, selectTab] = useUrlTab<TrackerTab>(TABS, 'connection')
  const [draft, setDraft] = useState<Draft>(() => draftOf(conn))
  const { busy, formError, run } = useSubmit()
  const [confirm, confirmDialog] = useConfirm()
  const settings = useMemo(() => patchOf(conn, draft), [conn, draft])
  const dirty = Object.keys(settings).length > 0
  const problem = problemOf(draft, containerNoun(conn.tracker))
  const otherOrg = useOtherOrg(conn.org)
  useActAsRows([{ path: `/api/tracker-connections/${conn.id}`, org: otherOrg }])

  const save = async (): Promise<boolean> => {
    if (!dirty || problem) return false
    // The server's field prefix ("mappings: …") is for code; a person reads the sentence after it.
    const mapError = (err: unknown, fallback: string) => apiDetail(err, fallback).replace(/^(mappings|start_label|done_state):\s*/, '')
    const landed = await run('Could not save — try again', () => updateTrackerConnection(conn.id, settings), { mapError })
    if (landed === null) return false
    client.setQueryData(trackerKey(conn.id), landed.value)
    void client.invalidateQueries({ queryKey: TRACKERS_KEY })
    return true
  }
  const guard = useLeaveGuard(dirty, save, ['tab'])

  const [webhook, setWebhook] = useState<{ ingress_url: string; webhook_secret: string } | null>(null)
  const rotateWebhook = async () => {
    let answer: { ingress_url: string; webhook_secret: string } | null = null
    const yes = await confirm({
      title: 'New webhook URL?',
      message: `The current webhook URL and secret stop working now. Update them in ${trackerLabel(conn.tracker)}.`,
      identity: conn.name,
      confirmLabel: 'New webhook URL',
      run: {
        action: async () => { answer = await rotateTrackerWebhook(conn.id) },
        failed: 'Could not make a new webhook URL — try again',
      },
    })
    if (yes && answer) {
      setWebhook(answer)
      void client.invalidateQueries({ queryKey: ['tracker-webhooks', conn.id] })
    }
  }

  const takeOwnership = async (automationId: string) => {
    const landed = await run('Could not take ownership — try again', () => takeAutomationOwnership(automationId))
    if (landed === null) return
    void client.invalidateQueries({ queryKey: trackerKey(conn.id) })
    void client.invalidateQueries({ queryKey: TRACKERS_KEY })
  }

  const disconnect = async () => {
    const yes = await confirm(danger('Disconnect', {
      title: 'Disconnect tracker?',
      message: 'Its automation and event log go. Tasks it started stay.',
      // A default name already starts with the tracker's label ("Jira · site"): say it once.
      identity: conn.name === trackerLabel(conn.tracker) || conn.name.startsWith(`${trackerLabel(conn.tracker)} · `)
        ? conn.name : `${trackerLabel(conn.tracker)} · ${conn.name}`,
      run: { action: () => deleteTrackerConnection(conn.id), failed: 'Could not disconnect — try again' },
    }))
    if (!yes) return
    void client.invalidateQueries({ queryKey: TRACKERS_KEY })
    client.removeQueries({ queryKey: trackerKey(conn.id) })
    guard.leaveNow(TRACKERS_PATH)
  }

  return (
    <DetailPage
      testId="tracker-detail"
      breadcrumb={crumbs}
      titleAside={<OrgAside label="Org" org={conn.org} chipTestId="tracker-org" />}
      actions={
        <OverflowMenu
          ariaLabel={`${conn.name} actions`}
          testId="tracker-actions"
          items={[{ key: 'disconnect', label: 'Disconnect', ariaLabel: `Disconnect ${conn.name}`, danger: true, testId: 'tracker-disconnect', onClick: () => void disconnect() }]}
        />
      }
      actionBar={EDITABLE.includes(tab) && (
        <CancelSaveBar
          testId="tracker-form-actions"
          saveTestId="tracker-save"
          discardTestId="tracker-discard"
          dirty={dirty}
          busy={busy}
          disabled={problem !== null}
          saveReason={dirty && problem ? problem : undefined}
          onDiscard={() => setDraft(draftOf(conn))}
          saveProps={{ form: FORM_ID }}
        />
      )}
    >
      <ActingForNotice org={otherOrg} testId="tracker-acting-for" />
      {conn.owner_outside_org && conn.automation_id && (
        <Banner
          severity="warning"
          testId="tracker-owner-outside"
          action={!otherOrg &&
            <Button type="button" color="inherit" size="small" disabled={busy} sx={tapTargetSx}
              data-testid="tracker-take-ownership" onClick={() => void takeOwnership(conn.automation_id as string)}>
              Take ownership
            </Button>
          }
        >
          Tracker starts are refused: this connection’s automation belongs to someone outside {conn.org?.name ?? 'this org'}.
          {otherOrg && ' An Owner or Admin there takes ownership.'}
        </Banner>
      )}
      {formError && <Banner severity="error" testId="tracker-save-error">{formError}</Banner>}
      <Tabs tabs={TABS} tab={tab} onSelect={selectTab} label="Tracker sections" idPrefix="tracker" />
      <FormBox id={FORM_ID} onSubmit={save}>
        <TabPanel id="connection" current={tab} idPrefix="tracker">
          <ConnectionTab conn={conn} onNewWebhook={() => void rotateWebhook()} />
          <WebhookSection id={conn.id} appManaged={conn.tracker === 'linear' || conn.auth_kind === 'forge'} />
        </TabPanel>
        <TabPanel id="mapping" current={tab} idPrefix="tracker">
          <MappingTab conn={conn} otherOrg={otherOrg} draft={draft} setDraft={setDraft} disabled={busy} />
        </TabPanel>
        <TabPanel id="people" current={tab} idPrefix="tracker">
          <Box sx={DETAIL_SECTIONS_SX}>
            <SettingCheckbox
              name="allow_unmapped_users"
              label="Non-members can start tasks"
              checked={draft.allow_unmapped_users}
              disabled={busy}
              onChange={checked => setDraft(d => ({ ...d, allow_unmapped_users: checked }))}
              testId="tracker-allow-unmapped"
            />
          </Box>
        </TabPanel>
      </FormBox>
      <TabPanel id="events" current={tab} idPrefix="tracker">
        <EventsTab id={conn.id} />
      </TabPanel>
      {confirmDialog}
      <SecretOnceDialog
        secret={webhook?.webhook_secret ?? null}
        title={`${conn.name}: new webhook`}
        notice={`Paste the URL and secret into ${trackerLabel(conn.tracker)}’s webhook settings. You won’t see them again.`}
        submitLabel="Done"
        testIdPrefix="tracker-webhook"
        onClose={() => setWebhook(null)}
      >
        {webhook && <WebhookUrlField url={webhook.ingress_url} testId="tracker-webhook-url" />}
      </SecretOnceDialog>
      <LeaveGuardDialog leave={guard} message="This tracker has unsaved changes. Save them before leaving?" />
    </DetailPage>
  )
}

const hasOwnWebhook = (conn: TrackerConnection) => conn.has_own_webhook

function ConnectionTab({ conn, onNewWebhook }: { conn: TrackerConnection; onNewWebhook: () => void }): JSX.Element {
  return (
    <Box sx={DETAIL_SECTIONS_SX}>
      <DetailSection
        fields
        testId="tracker-connection"
        actions={hasOwnWebhook(conn) && (
          <Button type="button" variant="outlined" size="small" sx={tapTargetSx} data-testid="tracker-new-webhook" onClick={onNewWebhook}>
            New webhook URL
          </Button>
        )}
      >
        <DetailField label="Name">{conn.name}</DetailField>
        <DetailField label="Tracker">{trackerLabel(conn.tracker)}</DetailField>
        <DetailField label="Auth">{authLabel(conn.auth_kind)}</DetailField>
        <DetailField label="Site">{conn.site ?? '—'}</DetailField>
        <DetailField label="Workspace">{conn.external_org_id ?? '—'}</DetailField>
        {conn.auth_kind === 'forge' && (
          <DetailField label="Jira app">
            {conn.claimed
              ? 'Claimed'
              : <>Waiting for the code{conn.install_url && <> · <ExternalLink href={conn.install_url} data-testid="tracker-install">Install</ExternalLink></>}</>}
          </DetailField>
        )}
        <DetailField label="Automation">
          {conn.automation_id
            ? <Link component={RouterLink} to={`/automations/${encodeURIComponent(conn.automation_id)}`} underline="hover" data-testid="tracker-automation">Open</Link>
            : '—'}
        </DetailField>
        <DetailField label="Added">{absoluteTime(conn.created_at)}</DetailField>
      </DetailSection>
    </Box>
  )
}

/* On a phone the mapping row restacks as a card (`listStyles.phoneCardSx`): the tracker-project picker takes the whole
   first line, the project picker and Remove share the second, each picker as wide as its line. */
const FIELD_FILLS_CELL = { '& .MuiFormControl-root': { flex: '1 1 auto', width: '100%', minWidth: 0 } }
const MAPPING_SX = phoneCardSx(phoneColumnOrder([
  ['container', { flex: '1 1 100%', pt: 1, ...FIELD_FILLS_CELL, '& > *': { flex: '1 1 auto', minWidth: 0 } }],
  ['project', { flex: '1 1 0', flexWrap: 'wrap', py: 1, ...FIELD_FILLS_CELL }],
  'remove',
]))

function MappingTab({ conn, otherOrg, draft, setDraft, disabled }: {
  conn: TrackerConnection
  otherOrg: OrgRef | null
  draft: Draft
  setDraft: (update: (d: Draft) => Draft) => void
  disabled: boolean
}): JSX.Element {
  // Another org's connection reads that org's roster as that org, so the operator's org filter cannot narrow it away.
  const ownRoster = useProjects({ staleTime: 60_000, enabled: otherOrg === null })
  const orgRoster = useQuery({
    // The key `useHomeProjects` (`pages/credentials/credentialForm.tsx`) reads the same roster under.
    queryKey: [...PROJECTS_KEY, { actAs: otherOrg?.id }],
    queryFn: () => get<Project[]>('/api/projects', ...readAsOnce(otherOrg)),
    staleTime: 60_000,
    enabled: otherOrg !== null,
  })
  const projects = otherOrg === null ? ownRoster : orgRoster
  // Only the connection's org's projects map (an operator's roster spans every org); a row whose payload carries no
  // `org` field is kept, as `ui/OrgIndicator.tsx::useOwnOrgRows` keeps it.
  const ownProjects = (projects.data ?? []).filter(p => !conn.org || p.org === undefined || p.org?.id === conn.org.id)
  // One read per mount: a 409 (no adapter yet) or a failure leaves a typed container id, its reason as the hint.
  const containers = useApiRead(['tracker-containers', conn.id], () => listTrackerContainers(conn.id), `Could not load ${containerNoun(conn.tracker)}s`)
  const projectOptions = [
    { value: '', label: 'Pick a project' },
    ...ownProjects.map(p => ({ value: String(p.id), label: p.name })).sort((a, b) => a.label.localeCompare(b.label)),
  ]
  const setRow = (key: number, patch: Partial<MappingRow>) =>
    setDraft(d => ({ ...d, mappings: d.mappings.map(r => (r.key === key ? { ...r, ...patch } : r)) }))

  // Saved pairs start tasks (the admin wire lists only primary links); a pair edited in this draft does not, yet.
  const saved = new Set((conn.settings?.mappings ?? []).map(m => `${m.container_id.trim()}\u0000${m.project_id}`))

  return (
    <Box sx={DETAIL_SECTIONS_SX}>
      <DetailSection
        title="Projects"
        actions={
          <Button type="button" variant="outlined" size="small" disabled={disabled} sx={tapTargetSx} data-testid="tracker-mapping-add"
            onClick={() => setDraft(d => ({ ...d, mappings: [...d.mappings, { key: rowKey(), container_id: '', project_id: '', editing: true }] }))}>
            Add
          </Button>
        }
      >
        {draft.mappings.length === 0 ? <EmptyState inline title="No projects mapped." />
          : (
            <Box sx={MAPPING_SX}>
            <DataTable ariaLabel="Mapping" testId="tracker-mapping">
              <TableHead>
                <TableRow>
                  <TableCell>{containerNoun(conn.tracker)}</TableCell>
                  <TableCell>Project</TableCell>
                  <TableCell padding="none" />
                </TableRow>
              </TableHead>
              <TableBody>
                {draft.mappings.map(row => {
                  const containerName = containers.data?.find(c => c.id === row.container_id)?.name ?? row.container_id
                  const edit = () => { if (!row.editing) setRow(row.key, { editing: true }) }
                  const rowContainers = row.editing ? containers.data : containers.data?.filter(c => c.id === row.container_id) ?? null
                  const rowProjects = row.editing ? projectOptions : projectOptions.filter(o => o.value === row.project_id)
                  return (
                  <TableRow key={row.key} data-testid="tracker-mapping-row" onFocusCapture={edit} onPointerDownCapture={edit}>
                    <TableCell data-col="container">
                      <ContainerSelect containers={rowContainers} error={containers.error} value={[row.container_id]}
                        label={containerNoun(conn.tracker)} disabled={disabled} testId="tracker-container" onChange={ids => setRow(row.key, { container_id: ids[0] ?? '' })} />
                    </TableCell>
                    <TableCell data-col="project">
                      <SelectField dense ariaLabel="Project" value={row.project_id} options={rowProjects.length ? rowProjects : projectOptions.slice(0, 1)} disabled={disabled}
                        onChange={value => setRow(row.key, { project_id: value })} testId="tracker-project-select" />
                      {saved.has(`${row.container_id.trim()}\u0000${row.project_id}`) && <StartsTasksChip />}
                    </TableCell>
                    <TableCell data-col="remove" padding="none" sx={{ textAlign: 'right' }}>
                      <IconButton aria-label={`Remove ${containerName || 'row'}`} disabled={disabled} sx={tapTargetSx}
                        onClick={() => setDraft(d => ({ ...d, mappings: d.mappings.filter(r => r.key !== row.key) }))}>
                        <CloseIcon />
                      </IconButton>
                    </TableCell>
                  </TableRow>
                  )
                })}
              </TableBody>
            </DataTable>
            </Box>
          )}
      </DetailSection>
      <DetailSection title="Labels">
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, maxWidth: grid(64) }}>
          <FormTextField label="Start label" value={draft.start_label} disabled={disabled} placeholder={DEFAULT_START_LABEL}
            help="Adding this label to an issue starts a task."
            onChange={value => setDraft(d => ({ ...d, start_label: value }))} testId="tracker-start-label" />
          <FormTextField label="Done state" value={draft.done_state} disabled={disabled} placeholder="Done"
            help="The issue moves here when the task's PR merges."
            onChange={value => setDraft(d => ({ ...d, done_state: value }))} testId="tracker-done-state" />
        </Box>
      </DetailSection>
    </Box>
  )
}

const WEBHOOK_TONE: Record<string, Tone> = { ok: 'success', stale: 'error', off: 'warning', missing: 'warning', unknown: 'neutral' }
const WEBHOOK_LABEL: Record<string, string> = { ok: 'Working', stale: 'Old address', off: 'Off', missing: 'Missing', unknown: 'Unknown' }

/* A tracker whose app registers the webhook (Linear, the Jira app) has no check to read: an event this recent is
   proof it works. Backlog and API-token Jira answer checks (none while nothing is mapped), so they get no State. */
const RECENT_EVENT_MS = 7 * 24 * 60 * 60 * 1000
const recentlyHeard = (at: string | null) => at !== null && Date.now() - Date.parse(at) < RECENT_EVENT_MS

/** The connection's event log, newest first: one read the Events tab and the Webhook section's "Last event" share. */
function useTrackerEvents(id: string) {
  return useApiRead(['tracker-events', id], () => listTrackerEvents(id), 'Could not load events')
}

/** The newest `received_at` among `events` (the Events tab's rows), or null when there is none. */
function newestEventAt(events: readonly TrackerEvent[]): string | null {
  let newest: string | null = null
  for (const event of events) {
    if (event.received_at && (newest === null || Date.parse(event.received_at) > Date.parse(newest))) newest = event.received_at
  }
  return newest
}

/** When the connection last heard an event (the newest row of its Events tab), and each webhook place's state, read
 *  live from the tracker. The two load apart: the live check may take seconds, the log answers at once. */
function WebhookSection({ id, appManaged }: { id: string; appManaged: boolean }): JSX.Element {
  const read = useApiRead(['tracker-webhooks', id], () => getTrackerWebhooks(id), 'Could not check the webhook')
  const events = useTrackerEvents(id)
  const data = read.data
  const last = events.data === null ? null : newestEventAt(events.data)
  return (
    <Box sx={{ ...DETAIL_SECTIONS_SX, mt: 2 }}>
      <DetailSection title="Webhook" fields testId="tracker-webhooks">
        <DetailField label="Last event">
          <span data-testid="tracker-last-event">
            {events.data === null
              ? events.error || 'Checking…'
              : last
                ? <Tip title={absoluteTime(last)}><span>{relativeAge(last)}</span></Tip>
                : 'None yet'}
          </span>
        </DetailField>
        {appManaged && data !== null && data.checks.length === 0 && (
          <DetailField label="State">
            <span data-testid="tracker-webhook-managed">
              {recentlyHeard(last) ? <StatusChip label="Working" tone="success" /> : 'Managed by the app'}
            </span>
          </DetailField>
        )}
        {read.error && data === null
          ? <DetailField label="State">{read.error}</DetailField>
          : data === null
            ? <DetailField label="State">Checking…</DetailField>
            : data.checks.map(check => (
              <DetailField key={check.where} label={check.where}>
                <Box data-testid="tracker-webhook-check" data-state={check.state}
                  sx={{ display: 'flex', flexDirection: 'column', gap: 0.5, minWidth: 0 }}>
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
                    <StatusChip label={WEBHOOK_LABEL[check.state] ?? check.state} tone={WEBHOOK_TONE[check.state] ?? 'neutral'} />
                    {check.state !== 'ok' && <span>{check.detail}</span>}
                  </Box>
                  {check.url && check.state !== 'ok' && (
                    <CopyableValue value={check.url} copyLabel="Copy address" testId="tracker-webhook-paste-url"
                      sx={{ overflowWrap: 'anywhere' }} />
                  )}
                </Box>
              </DetailField>
            ))}
      </DetailSection>
    </Box>
  )
}

/* On a phone each event restacks as a card (`listStyles.phoneCardSx`): issue, outcome, task and age on the first line,
   what happened and the reason each on a line of their own; an empty task or reason draws nothing. */
const EVENTS_SX = phoneCardSx({
  ...phoneColumnOrder([
    ['issue', { whiteSpace: 'nowrap' }],
    'outcome',
    'task',
    ['when', { marginLeft: 'auto' }],
    ['what', { flex: '1 1 100%' }],
    ['reason', { flex: '1 1 100%' }],
  ]),
  '& tbody td[data-empty]': { display: 'none' },
}, { py: 1, rowGap: 0.5 })

function EventsTab({ id }: { id: string }): JSX.Element {
  const events = useTrackerEvents(id)
  if (events.error && events.data === null) return <LoadFailedState title="Events unavailable" error={events.error} onRetry={() => void events.reload()} />
  if (events.data === null) return <LoadingState rows={4} />
  if (events.data.length === 0) return <EmptyState title="No events yet" />
  return (
    <Box sx={[DETAIL_SECTIONS_SX, EVENTS_SX]}>
      <DataTable ariaLabel="Events" testId="tracker-events">
        <TableHead>
          <TableRow>
            <TableCell>When</TableCell>
            <TableCell>Issue</TableCell>
            <TableCell>What</TableCell>
            <TableCell>Outcome</TableCell>
            <TableCell>Reason</TableCell>
            <TableCell>Task</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {events.data.map(event => {
            const p = eventPreview(event.preview)
            const what = [p?.kind, p?.actor].filter(Boolean).join(' · ')
            return (
              <TableRow key={event.delivery_id} data-testid="tracker-event-row" data-state={event.state}>
                <TableCell data-col="when" sx={{ whiteSpace: 'nowrap' }}><Tip title={absoluteTime(event.received_at)}><span>{compactAge(event.received_at)}</span></Tip></TableCell>
                <TableCell data-col="issue">
                  {p?.issue_key
                    ? p.issue_url ? <ExternalLink href={p.issue_url}>{p.issue_key}</ExternalLink> : p.issue_key
                    : '—'}
                </TableCell>
                <TableCell data-col="what" sx={wordsSx}>{p?.text ? <Tip title={p.text}><span>{what || '—'}</span></Tip> : (what || '—')}</TableCell>
                <TableCell data-col="outcome">
                  <StatusChip label={event.outcome}
                    tone={EVENT_TONE[event.state] ?? 'neutral'} data-status={event.state} />
                </TableCell>
                <TableCell data-col="reason" data-empty={event.reason ? undefined : ''} sx={wordsSx}>{event.reason || '—'}</TableCell>
                <TableCell data-col="task" data-empty={event.task_id !== null ? undefined : ''}>{event.task_id !== null ? <><CellLabel>Task</CellLabel><TaskLink taskId={event.task_id} /></> : '—'}</TableCell>
              </TableRow>
            )
          })}
        </TableBody>
      </DataTable>
    </Box>
  )
}
