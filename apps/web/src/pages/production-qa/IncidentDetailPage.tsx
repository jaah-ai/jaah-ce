import { useState, type FormEvent, type JSX } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Link as RouterLink, useParams } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Link from '@mui/material/Link'
import ExternalLink from '../../ui/ExternalLink'
import Typography from '@mui/material/Typography'
import { apiDetail } from '../../api'
import Breadcrumb from '../../components/Breadcrumb'
import { Tabs, TabPanel } from '../../components/Tabs'
import { useUrlTab } from '../../hooks/useUrlTab'
import { humanize } from '../../lib/humanize'
import { absoluteTime } from '../../lib/relativeTime'
import { safeHref } from '../../lib/safeHref'
import {
  CONFIDENCE,
  INCIDENT_STATE,
  INCIDENT_TYPE,
  SEVERITY,
  addSuspect,
  decideSuspect,
  getIncident,
  prName,
  wordOf,
  type IncidentDetail,
  type Suspect,
  type SuspectDecision,
} from '../../lib/productionQa'
import { shortSha } from '../../lib/shortSha'
import { TaskLink } from '../../ui/DetailFields'
import DetailPage, { DETAIL_SECTIONS_SX, DetailField, DetailSection } from '../../ui/detail/DetailPage'
import { detailStatus } from '../../ui/detail/DetailStatus'
import EmptyState from '../../ui/EmptyState'
import { FactsGrid } from '../../ui/FactsGrid'
import FormTextField from '../../ui/FormTextField'
import { StatusChip } from '../../ui/StatusChip'
import { FONT_SIZE, grid, SIZE } from '../../theme'
import { EdgeStatus, MONO_SX, Sent, ToneWord, useDetailRead } from './shared'
import Tip from '../../ui/Tip'

/* ONE INCIDENT (`GET /api/incidents/{id}`), opened from Incidents. A tabbed detail (ui-style-guide), read-only:
 * Overview — every recorded field, a field the vendor did not send reading "Not sent" (AC-9), and the stack frames;
 * Suspects — the PRs linked to it with basis and confidence as words; a project editor confirms, dismisses or adds
 * one, and each decision shows who made it and when (rules: `apps/api/app/production_qa/linkage.py`);
 * History — its events; Tasks — the tasks filed for it; Ledger — the facts written for the outcome ledger, each
 * "Pending" until the ledger applies it. Polls every 15 s while visible. */

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'suspects', label: 'Suspects' },
  { id: 'history', label: 'History' },
  { id: 'tasks', label: 'Tasks' },
  { id: 'ledger', label: 'Ledger' },
] as const
type IncidentTab = (typeof TABS)[number]['id']

export default function IncidentDetailPage(): JSX.Element {
  const { id = '' } = useParams<{ id: string }>()
  const [tab, selectTab] = useUrlTab<IncidentTab>(TABS, 'overview')
  const read = useDetailRead('incident', id, getIncident)
  const i = read.data
  const crumbs = <Breadcrumb items={[{ label: 'Incidents', to: '/incidents' }, { label: i?.title ?? 'Incident' }]} />

  if (!i) return detailStatus(read, crumbs, 'incident')
  const state = wordOf(INCIDENT_STATE, i.state)
  return (
    <DetailPage
      testId="incident-detail"
      breadcrumb={crumbs}
      titleAside={<StatusChip label={state.label} tone={state.tone} data-status={i.state} testId="incident-detail-state" />}
    >
      <Tabs tabs={TABS} tab={tab} onSelect={selectTab} label="Incident sections" idPrefix="incident" overflowMenu />
      <TabPanel id="overview" current={tab} idPrefix="incident"><Overview i={i} /></TabPanel>
      <TabPanel id="suspects" current={tab} idPrefix="incident"><Suspects i={i} /></TabPanel>
      <TabPanel id="history" current={tab} idPrefix="incident"><History i={i} /></TabPanel>
      <TabPanel id="tasks" current={tab} idPrefix="incident"><Tasks i={i} /></TabPanel>
      <TabPanel id="ledger" current={tab} idPrefix="incident"><Ledger i={i} /></TabPanel>
    </DetailPage>
  )
}

function Overview({ i }: { i: IncidentDetail }): JSX.Element {
  const severity = wordOf(SEVERITY, i.severity)
  return (
    <Box sx={DETAIL_SECTIONS_SX}>
      <FactsGrid minTile={grid(29)} testId="incident-overview">
        <DetailField label="Type">{INCIDENT_TYPE[i.type] ?? i.type}</DetailField>
        <DetailField label="Severity" testId="incident-detail-severity"><StatusChip label={severity.label} tone={severity.tone} data-status={i.severity} /></DetailField>
        <DetailField label="Project">{i.project.name}</DetailField>
        <DetailField label="Environment" testId="incident-environment"><Sent value={i.environment} /></DetailField>
        <DetailField label="Count">{`${i.count}${i.count_mode === 'absolute' ? ' (vendor total)' : ''}`}</DetailField>
        <DetailField label="First seen">{absoluteTime(i.first_seen)}</DetailField>
        <DetailField label="Last seen">{absoluteTime(i.last_seen)}</DetailField>
        {i.resolved_at && <DetailField label="Resolved">{absoluteTime(i.resolved_at)}</DetailField>}
        <DetailField label="Reported release" testId="incident-release"><Sent value={i.release}><Box component="span" sx={MONO_SX}>{i.release}</Box></Sent></DetailField>
        <DetailField label="Commit" testId="incident-commit">
          <Sent value={i.commit_sha}><Tip title={i.commit_sha ?? undefined}><Box component="span" sx={MONO_SX}>{shortSha(i.commit_sha)}</Box></Tip></Sent>
        </DetailField>
        <DetailField label="Source">{i.source_kind}</DetailField>
        <DetailField label="Vendor link" testId="incident-vendor-url">
          <Sent value={i.vendor_url}>
            {safeHref(i.vendor_url) && <ExternalLink href={i.vendor_url}>{i.vendor_ref ?? 'Open'}</ExternalLink>}
          </Sent>
        </DetailField>
        <DetailField label="Vendor ref" testId="incident-vendor-ref"><Sent value={i.vendor_ref} /></DetailField>
        <DetailField label="Jaah release" testId="incident-deployment">
          {i.deployment_id === null ? '—' : <Link component={RouterLink} to={`/deployments/${i.deployment_id}`} underline="hover">{`#${i.deployment_id}`}</Link>}
        </DetailField>
        <DetailField label="Fingerprint" fullRow><Box component="span" sx={MONO_SX}>{i.fingerprint}</Box></DetailField>
      </FactsGrid>
      <DetailSection title="Stack" testId="incident-stack">
        {i.stack_frames === null || i.stack_frames.length === 0 ? (
          <Sent value={null} />
        ) : (
          <Box component="ol" sx={[...MONO_SX, { m: 0, pl: 3, fontSize: FONT_SIZE.md, display: 'flex', flexDirection: 'column', gap: 0.5 }]}>
            {i.stack_frames.map((f, n) => (
              <Box component="li" key={n} data-testid="incident-frame" sx={{ color: f.in_app === false ? 'text.secondary' : 'text.primary' }}>
                {`${f.function ?? '?'} — ${f.path ?? '?'}${f.lineno !== null ? `:${f.lineno}` : ''}`}
              </Box>
            ))}
          </Box>
        )}
      </DetailSection>
    </Box>
  )
}

const DECISION_WORD: Record<string, string> = { confirmed: 'Confirmed', dismissed: 'Dismissed', suggested: 'Undone' }

/** Corrections answer the incident; the page's read is replaced with it, so the change shows at once. */
function useSuspectWrite(i: IncidentDetail) {
  const client = useQueryClient()
  const [error, setError] = useState('')
  const settle = {
    onSuccess: (next: IncidentDetail) => { setError(''); client.setQueryData(['incident', String(i.id)], next) },
    onError: (e: unknown) => setError(apiDetail(e, 'Could not save the change')),
  }
  const decide = useMutation({ mutationFn: (a: { id: number; state: SuspectDecision }) => decideSuspect(i.id, a.id, a.state), ...settle })
  const add = useMutation({ mutationFn: (a: { repo: string; pr: number }) => addSuspect(i.id, a.repo, a.pr), ...settle })
  return { decide, add, error, busy: decide.isPending || add.isPending }
}

function Suspects({ i }: { i: IncidentDetail }): JSX.Element {
  const write = useSuspectWrite(i)
  const manyRepos = new Set(i.suspects.map(s => s.repo_path)).size > 1
  return (
    <Box sx={DETAIL_SECTIONS_SX}>
      {i.suspects.length === 0 ? (
        <EmptyState title="No suspect" />
      ) : (
        <DetailSection fields testId="incident-suspects">
          {i.suspects.map(s => (
            <DetailField key={s.id} label={prName(s, manyRepos)} wide testId="incident-suspect-row">
              <SuspectRow s={s} canEdit={Boolean(i.can_edit)} busy={write.busy} onDecide={state => write.decide.mutate({ id: s.id, state })} />
            </DetailField>
          ))}
        </DetailSection>
      )}
      {write.error && <Typography role="alert" variant="body2" sx={{ color: 'error.main' }} data-testid="suspect-error">{write.error}</Typography>}
      {i.can_edit && (
        <AddSuspect
          defaultRepo={i.suspects[0]?.repo_path ?? ''}
          busy={write.busy}
          onAdd={(repo, pr) => write.add.mutate({ repo, pr })}
        />
      )}
    </Box>
  )
}

function SuspectRow({ s, canEdit, busy, onDecide }: {
  s: Suspect
  canEdit: boolean
  busy: boolean
  onDecide: (state: SuspectDecision) => void
}): JSX.Element {
  const confidence = wordOf(CONFIDENCE, s.confidence)
  const decided = s.decided_at !== null
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.5 }}>
      <Box component="span" sx={{ display: 'inline-flex', flexWrap: 'wrap', columnGap: 1, opacity: s.state === 'dismissed' ? 0.6 : 1 }}>
        <ToneWord label={`${confidence.label} confidence`} tone={confidence.tone} status={s.confidence} />
        <span>{`· ${humanize(s.basis)} · ${humanize(s.state)}`}</span>
        {s.task_id !== null && <TaskLink taskId={s.task_id} label="Task" />}
        {s.deployment_id !== null && (
          <Link component={RouterLink} to={`/deployments/${s.deployment_id}`} underline="hover">{`Release #${s.deployment_id}`}</Link>
        )}
      </Box>
      {decided && (
        <Box component="span" data-testid="incident-suspect-decided" sx={{ color: 'text.secondary', fontSize: FONT_SIZE.md }}>
          {`${DECISION_WORD[s.state] ?? humanize(s.state)} by ${s.decided_by_name || 'someone'} · ${absoluteTime(s.decided_at as string)}`}
        </Box>
      )}
      {canEdit && (
        <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1 }}>
          {s.state !== 'confirmed' && (
            <Button size="small" variant="outlined" disabled={busy} onClick={() => onDecide('confirmed')} data-testid="suspect-confirm">Confirm</Button>
          )}
          {s.state !== 'dismissed' && (
            <Button size="small" variant="outlined" color="inherit" disabled={busy} onClick={() => onDecide('dismissed')} data-testid="suspect-dismiss">Dismiss</Button>
          )}
          {decided && s.basis !== 'operator' && s.state !== 'suggested' && (
            <Button size="small" color="inherit" disabled={busy} onClick={() => onDecide('suggested')} data-testid="suspect-undo">Undo</Button>
          )}
        </Box>
      )}
    </Box>
  )
}

/** The add-a-suspect fields' explanations: what adding does (`linkage.add` in the API). */
const ADD_SUSPECT_HELP = {
  repo: { tip: 'Repo of the pull request to blame.' },
  pr: {
    tip: 'Pull request added as a confirmed suspect.',
    more: 'An added suspect is confirmed at once, with high confidence. When this project shipped the PR in a '
      + 'release, its task and release link to the incident. A PR already listed is confirmed in place.',
    docs: 'using-jaah/production-qa/how-production-qa-works',
  },
} as const

function AddSuspect({ defaultRepo, busy, onAdd }: { defaultRepo: string; busy: boolean; onAdd: (repo: string, pr: number) => void }): JSX.Element {
  const [repo, setRepo] = useState(defaultRepo)
  const [pr, setPr] = useState('')
  const number = Number(pr)
  const valid = repo.trim() !== '' && Number.isInteger(number) && number > 0
  const submit = (e: FormEvent) => {
    e.preventDefault()
    if (valid) { onAdd(repo.trim(), number); setPr('') }
  }
  return (
    <DetailSection title="Add a suspect" testId="incident-suspect-add">
      <Box component="form" onSubmit={submit} sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, alignItems: 'center' }}>
        <FormTextField label="Repository" help={ADD_SUSPECT_HELP.repo} placeholder="owner/name" value={repo} onChange={setRepo} sx={{ flex: `1 1 ${grid(33)}`, maxWidth: SIZE.measureNarrow }} testId="suspect-add-repo" slotProps={{ htmlInput: { maxLength: 512 } }} />
        <FormTextField label="PR" help={ADD_SUSPECT_HELP.pr} value={pr} onChange={v => setPr(v.replace(/[^0-9]/g, ''))} sx={{ width: grid(16) }} testId="suspect-add-pr" slotProps={{ htmlInput: { inputMode: 'numeric' } }} />
        <Button type="submit" size="small" variant="contained" disabled={!valid || busy} data-testid="suspect-add-submit">Add</Button>
      </Box>
    </DetailSection>
  )
}

function History({ i }: { i: IncidentDetail }): JSX.Element {
  if (i.events.length === 0) return <Box sx={DETAIL_SECTIONS_SX}><EmptyState title="No history" /></Box>
  return (
    <Box sx={DETAIL_SECTIONS_SX}>
      <DetailSection fields testId="incident-history">
        {[...i.events].reverse().map(e => (
          <DetailField key={e.id} label={absoluteTime(e.at)} wide testId="incident-event">
            {`${humanize(e.kind)}${e.note ? ` — ${e.note}` : ''}`}
          </DetailField>
        ))}
      </DetailSection>
    </Box>
  )
}

function Tasks({ i }: { i: IncidentDetail }): JSX.Element {
  // Why the project's revert policy filed no revert (no regression automation, no suspect PR).
  const revert = i.revert_note
    ? <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="incident-revert-note">{i.revert_note}</Typography>
    : null
  if (i.tasks.length === 0) return <Box sx={DETAIL_SECTIONS_SX}><EmptyState title="No tasks filed" action={revert} /></Box>
  return (
    <Box sx={DETAIL_SECTIONS_SX}>
      {revert}
      <DetailSection fields testId="incident-tasks">
        {i.tasks.map(t => (
          <DetailField key={`${t.role}-${t.claimed_at}`} label={humanize(t.role)} wide testId="incident-task">
            {t.task_id !== null ? <TaskLink taskId={t.task_id} /> : 'Filing'}
            {` · ${absoluteTime(t.claimed_at)}${t.released_at ? ` · released ${absoluteTime(t.released_at)}` : ''}`}
          </DetailField>
        ))}
      </DetailSection>
    </Box>
  )
}

function Ledger({ i }: { i: IncidentDetail }): JSX.Element {
  if (i.edges.length === 0) return <Box sx={DETAIL_SECTIONS_SX}><EmptyState title="No ledger facts" /></Box>
  return (
    <Box sx={DETAIL_SECTIONS_SX}>
      <DetailSection fields testId="incident-ledger">
        {i.edges.map(e => (
          <DetailField key={e.id} label={humanize(e.kind)} wide testId="incident-edge">
            <TaskLink taskId={e.task_id} />
            {' · '}
            <EdgeStatus edge={e} />
            {` · ${absoluteTime(e.occurred_at)}`}
          </DetailField>
        ))}
      </DetailSection>
    </Box>
  )
}
