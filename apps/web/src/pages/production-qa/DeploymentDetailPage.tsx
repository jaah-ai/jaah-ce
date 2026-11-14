import type { JSX } from 'react'
import { Link as RouterLink, useParams } from 'react-router-dom'
import Box from '@mui/material/Box'
import Link from '@mui/material/Link'
import Stack from '@mui/material/Stack'
import Breadcrumb from '../../components/Breadcrumb'
import { FullPageDetail } from '../../components/ListDetail'
import { isNotFound } from '../../api'
import { Tabs, TabPanel } from '../../components/Tabs'
import { useUrlTab } from '../../hooks/useUrlTab'
import { absoluteTime } from '../../lib/relativeTime'
import {
  DEPLOYMENT_STATUS,
  INCIDENT_STATE,
  METRIC_LABEL,
  getDeployment,
  metricDelta,
  metricValue,
  prName,
  rangeText,
  wordOf,
  type DeploymentDetail,
  type DeploymentMetric,
  type Probe,
  type ProbeResult,
  type ProbeRun,
} from '../../lib/productionQa'
import { humanize } from '../../lib/humanize'
import { shortSha } from '../../lib/shortSha'
import { monoSx } from '../../ui/listStyles'
import { FONT_SIZE, FONT_WEIGHT, grid, RADIUS } from '../../theme'
import { StatusChip } from '../../ui/StatusChip'
import { TaskLink } from '../../ui/DetailFields'
import ExternalLink from '../../ui/ExternalLink'
import DetailPage, { DETAIL_SECTIONS_SX, DetailField, DetailSection } from '../../ui/detail/DetailPage'
import { detailStatus } from '../../ui/detail/DetailStatus'
import EmptyState from '../../ui/EmptyState'
import { EdgeStatus, MONO_SX, ToneWord, useDetailRead } from './shared'
import AdvancedSection from '../../ui/AdvancedSection'
import Tip from '../../ui/Tip'

/* ONE DEPLOY RUN (`GET /api/deployments/{id}`), opened from a release's Run on the pipeline page (`/tasks`). A tabbed detail (ui-style-guide), read-only:
 * Overview — the commit range, the PRs it shipped (each to its task), commits no known PR carries, where a failed
 * run broke, the run and its event, and its received/started/finished times; Metrics — the post-deploy regression
 * window's measurements, or why there are none; Probes — the deploy's probe run, each health URL's and smoke
 * command's result from it (its output on open, its incident when one filed), then the `health_breach` incidents. Polls every 15 s while visible, as the list does. */

const TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'metrics', label: 'Metrics' },
  { id: 'probes', label: 'Probes' },
] as const
type DeploymentTab = (typeof TABS)[number]['id']

/** A 404: no run here yet, or none this viewer may see — say where runs come from, not just "not found". */
const RELEASE_NOT_FOUND = 'Release not found. Releases appear here once the project reports them.'

export default function DeploymentDetailPage(): JSX.Element {
  const { id = '' } = useParams<{ id: string }>()
  const [tab, selectTab] = useUrlTab<DeploymentTab>(TABS, 'overview')
  const read = useDetailRead('deployment', id, getDeployment)
  const d = read.data
  /* Its project's pipeline, where its release sits, then the environment. */
  const crumbs = (
    <Breadcrumb items={d
      ? [{ label: d.project.name, to: `/tasks?project=${d.project.id}` }, { label: d.environment }]
      : [{ label: 'Tasks', to: '/tasks' }, { label: 'Release' }]} />
  )

  if (!d) {
    const status = detailStatus(read, crumbs, 'release', RELEASE_NOT_FOUND)
    // Missing, it is the page: nothing to sit beside the board for.
    return read.isError && isNotFound(read.error) ? <><FullPageDetail />{status}</> : status
  }
  const status = wordOf(DEPLOYMENT_STATUS, d.status)
  return (
    <DetailPage
      testId="deployment-detail"
      breadcrumb={crumbs}
      titleAside={<StatusChip label={status.label} tone={status.tone} data-status={d.status} testId="deployment-detail-status" />}
    >
      <Tabs tabs={TABS} tab={tab} onSelect={selectTab} label="Release sections" idPrefix="deployment" />
      <TabPanel id="overview" current={tab} idPrefix="deployment">
        <Overview d={d} />
      </TabPanel>
      <TabPanel id="metrics" current={tab} idPrefix="deployment">
        <Metrics d={d} />
      </TabPanel>
      <TabPanel id="probes" current={tab} idPrefix="deployment">
        <Probes d={d} />
      </TabPanel>
    </DetailPage>
  )
}

function Overview({ d }: { d: DeploymentDetail }): JSX.Element {
  const manyRepos = new Set(d.prs.map(p => p.repo_path)).size > 1
  const prShas = new Set(d.prs.map(p => p.merge_sha))
  const loose = d.commits.filter(sha => !prShas.has(sha))
  // Each PR task's `deployed` ledger fact: Applied once the outcome ledger holds it, else Pending.
  const deployed = new Map(d.edges.filter(e => e.kind === 'deployed').map(e => [e.task_id, e]))
  return (
    <Box sx={DETAIL_SECTIONS_SX}>
      <DetailSection fields testId="deployment-overview">
        <DetailField label="Project">{d.project.name}</DetailField>
        <DetailField label="Environment">{d.environment}</DetailField>
        <DetailField label="Range" testId="deployment-detail-range">
          <Box component="span" sx={MONO_SX}>{rangeText(d)}</Box>
          {d.range_note && <Box component="span" sx={{ color: 'text.secondary' }}>{` — ${d.range_note}`}</Box>}
        </DetailField>
        <DetailField label="PRs" wide testId="deployment-detail-prs">
          {d.prs.length === 0 ? '—' : (
            <Box component="span" sx={{ display: 'flex', flexWrap: 'wrap', columnGap: 1.5 }}>
              {d.prs.map(pr => {
                const edge = pr.task_id === null ? undefined : deployed.get(pr.task_id)
                return (
                  <span key={`${pr.repo_path}#${pr.pr_number}`}>
                    {pr.task_id !== null
                      ? <TaskLink taskId={pr.task_id} label={prName(pr, manyRepos)} testId="deployment-pr-task" />
                      : prName(pr, manyRepos)}
                    {edge && <><Box component="span" sx={{ color: 'text.secondary' }}>{' · ledger '}</Box><EdgeStatus edge={edge} testId="deployment-pr-ledger" /></>}
                  </span>
                )
              })}
            </Box>
          )}
        </DetailField>
        {loose.length > 0 && (
          <DetailField label="Other commits" wide testId="deployment-loose-commits">
            <Tip title={loose.join('\n')}><Box component="span" sx={MONO_SX}>{loose.map(sha => shortSha(sha) ?? '').join(', ')}</Box></Tip>
          </DetailField>
        )}
        {(d.failing_job || d.failing_step) && (
          <DetailField label="Failed at" testId="deployment-failing">
            <ToneWord label={[d.failing_job, d.failing_step].filter(Boolean).join(' › ')} tone="error" />
          </DetailField>
        )}
        <DetailField label="Run" testId="deployment-run">
          <ExternalLink href={d.run_url}>{`${d.workflow_name ?? d.workflow_path} #${d.run_id}`}</ExternalLink>
          {d.run_attempt > 1 && ` (attempt ${d.run_attempt})`}
        </DetailField>
        <DetailField label="Event">{d.run_event ?? '—'}</DetailField>
        <DetailField label="Commit"><Tip title={d.head_sha}><Box component="span" sx={MONO_SX}>{shortSha(d.head_sha)}</Box></Tip></DetailField>
        <DetailField label="Received">{absoluteTime(d.first_received_at)}</DetailField>
        <DetailField label="Started">{absoluteTime(d.started_at)}</DetailField>
        <DetailField label="Finished">{absoluteTime(d.finished_at)}</DetailField>
      </DetailSection>
      {d.environments.length > 1 && (
        <DetailSection title="Environments" fields testId="deployment-environments">
          {d.environments.map(env => (
            <DetailField key={env.environment} label={env.environment}>
              {`${env.state}${env.job_name ? ` · ${env.job_name}` : ''}`}
            </DetailField>
          ))}
        </DetailSection>
      )}
    </Box>
  )
}

/** Why the window has no numbers, in words (AC-21/24): the recorder's note, or the window's own state. */
function notMeasured(d: DeploymentDetail): string {
  if (d.regression_status === 'pending') {
    return d.regression_due_at ? `Pending — measured ${absoluteTime(d.regression_due_at)}` : 'Pending'
  }
  return `Not measured: ${d.regression_note ?? (d.is_production ? 'no monitor reports this project' : 'not a production release')}`
}

function Metrics({ d }: { d: DeploymentDetail }): JSX.Element {
  if (d.metrics.length === 0) {
    return (
      <Box sx={DETAIL_SECTIONS_SX} data-testid="deployment-metrics-none">
        <EmptyState title={notMeasured(d)} />
      </Box>
    )
  }
  return (
    <Box sx={DETAIL_SECTIONS_SX} data-testid="deployment-metrics">
      {d.metrics.map(m => <MetricSection key={m.id} m={m} />)}
    </Box>
  )
}

function MetricSection({ m }: { m: DeploymentMetric }): JSX.Element {
  const label = METRIC_LABEL[m.metric] ?? m.metric
  const measured = m.baseline !== null || m.post !== null
  return (
    <DetailSection title={label} fields testId={`metric-${m.metric}`}>
      {measured ? (
        <>
          <DetailField label="Baseline" testId="metric-baseline">{metricValue(m.metric, m.baseline)}</DetailField>
          <DetailField label="After" testId="metric-post">{metricValue(m.metric, m.post)}</DetailField>
          <DetailField label="Change" testId="metric-delta">
            <ToneWord label={metricDelta(m)} tone={m.breached ? 'error' : 'neutral'} />
            {m.breached && <ToneWord label=" · Breached" tone="error" />}
          </DetailField>
          <DetailField label="Window" testId="metric-window">{`${m.window_min} min${m.events_post !== null ? ` · ${m.events_post} events` : ''}`}</DetailField>
        </>
      ) : (
        <DetailField label="Result" testId="metric-not-measured">{`Not measured: ${m.note ?? 'no data in the window'}`}</DetailField>
      )}
      {measured && m.note && <DetailField label="Note">{m.note}</DetailField>}
    </DetailSection>
  )
}

/* A probe's reported facts worth a glance: the HTTP status or exit code, and how long it took. */
function probeFacts(p: Probe): string {
  const f = p.facts
  const parts: string[] = []
  if (typeof f.status === 'number') parts.push(`HTTP ${f.status}`)
  if (typeof f.exit_code === 'number') parts.push(`exit ${f.exit_code}`)
  if (typeof f.duration_s === 'number') parts.push(`${f.duration_s}s`)
  return parts.join(' · ')
}

/** Why a run probed nothing or moved no incident (`probes.plan`'s `skipped`). */
const SKIPPED: Record<string, string> = {
  production_qa_off: 'Skipped: Production QA off',
  project_not_opted_in: 'Skipped: project not opted in',
  superseded: 'Skipped: a later release superseded it',
}

/** The probe run's state while it has not reported: a live run reads Running, anything else its own word. */
function runWord(run: ProbeRun): { label: string; tone: 'info' | 'error' | 'neutral' } {
  if (run.state === 'ready' || run.state === 'active') return { label: 'Running', tone: 'info' }
  return { label: humanize(run.state), tone: run.state === 'system_failed' ? 'error' : 'neutral' }
}

/** A result's facts: the HTTP status or exit code, and how long it took. */
function resultFacts(r: ProbeResult): string {
  const parts: string[] = []
  if (r.status !== null) parts.push(`HTTP ${r.status}`)
  if (r.exit_code !== null) parts.push(`exit ${r.exit_code}`)
  if (r.duration_s !== null) parts.push(`${r.duration_s}s`)
  return parts.join(' · ')
}

function ProbeResultRow({ r }: { r: ProbeResult }): JSX.Element {
  const facts = resultFacts(r)
  return (
    <Box component="li" data-testid="deployment-probe-result" data-ok={String(r.ok)} sx={{ minWidth: 0 }}>
      <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 1.5, rowGap: 0.5, minWidth: 0 }}>
        <StatusChip label={r.ok ? 'Passed' : 'Failed'} tone={r.ok ? 'success' : 'error'} />
        <Box component="span" sx={{ fontWeight: FONT_WEIGHT.medium, overflowWrap: 'anywhere' }}>{r.name}</Box>
        <Box component="span" sx={{ color: 'text.secondary' }}>
          {[r.kind === 'http' ? 'HTTP' : 'Command', facts].filter(Boolean).join(' · ')}
        </Box>
        {r.incident_id !== null && (
          <Link component={RouterLink} to={`/incidents/${r.incident_id}`} underline="hover" data-testid="deployment-probe-incident">
            {`Incident #${r.incident_id}`}
          </Link>
        )}
      </Box>
      {r.output_tail && (
        <AdvancedSection dense label="Output" testId="deployment-probe-output-fold">
          <Box component="pre" data-testid="deployment-probe-output"
            sx={[monoSx, { m: 0, p: 1, fontSize: FONT_SIZE.sm, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere',
              maxHeight: grid(42.5), overflow: 'auto', bgcolor: 'action.hover', borderRadius: RADIUS.sm }]}>
            {r.output_tail}
          </Box>
        </AdvancedSection>
      )}
    </Box>
  )
}

function Probes({ d }: { d: DeploymentDetail }): JSX.Element {
  const { probes, probe_run: run, probe_results: results } = d
  if (!run && results.length === 0 && probes.length === 0) {
    return <Box sx={DETAIL_SECTIONS_SX} data-testid="deployment-probes-none"><EmptyState title="No probe results" /></Box>
  }
  const pending = run !== null && run.reported_at === null
  return (
    <Box sx={DETAIL_SECTIONS_SX}>
      {run && (
        <DetailSection fields testId="deployment-probe-run">
          <DetailField label="Run">
            <TaskLink taskId={run.task_id} testId="deployment-probe-run-task" />
            {pending
              ? <>{' · '}<StatusChip {...runWord(run)} data-status={run.state} testId="deployment-probe-run-state" /></>
              : ` · ${absoluteTime(run.reported_at)}`}
            {run.skipped && <Box component="span" data-testid="deployment-probe-run-skipped">{` · ${SKIPPED[run.skipped] ?? humanize(run.skipped)}`}</Box>}
          </DetailField>
        </DetailSection>
      )}
      {results.length > 0 && (
        <DetailSection title="Results" divided={run !== null} testId="deployment-probe-results">
          <Stack component="ul" spacing={1} sx={{ m: 0, p: 0, listStyle: 'none' }}>
            {results.map(r => <ProbeResultRow key={r.probe_id} r={r} />)}
          </Stack>
        </DetailSection>
      )}
      {probes.length > 0 && <ProbeIncidents probes={probes} divided={run !== null || results.length > 0} />}
    </Box>
  )
}

/** The `health_breach` incidents on this deploy, each to its incident. */
function ProbeIncidents({ probes, divided }: { probes: Probe[]; divided: boolean }): JSX.Element {
  return (
    <DetailSection title="Incidents" divided={divided} fields testId="deployment-probes">
      {probes.map(p => {
        const failing = p.state !== 'resolved'
        const facts = probeFacts(p)
        const state = wordOf(INCIDENT_STATE, p.state)
        return (
          <DetailField key={p.incident_id} label={p.title} wide testId="deployment-probe">
            <StatusChip label={failing ? 'Failing' : 'Passing'} tone={failing ? 'error' : 'success'} data-status={p.state} />
            <Box component="span" sx={{ color: 'text.secondary' }}>
              {`${facts ? ` · ${facts}` : ''} · ${state.label} · `}
              <Link component={RouterLink} to={`/incidents/${p.incident_id}`} underline="hover">{absoluteTime(p.last_seen)}</Link>
            </Box>
          </DetailField>
        )
      })}
    </DetailSection>
  )
}
