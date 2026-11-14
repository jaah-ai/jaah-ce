// The Task page's Details › Outcome subtab (Architecture §9.3, PRD R5.4): the outcome ledger's rows for this task, one card per
// dispatch, with their attribution, timeline, gate facts and defect links. Reads `GET /api/tasks/{id}/outcomes`;
// the link decisions post to `/api/outcome-links/{id}/confirm|dismiss` (Caused by is the Task tab's `TaskPageFields`).
// Not here: the PR (Summary's Outcome card), and model, cost and tokens (the Metrics subtab).
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Chip from '@mui/material/Chip'
import Link from '@mui/material/Link'
import ExternalLink from '../../ui/ExternalLink'
import Stack from '@mui/material/Stack'
import Tip from '../../ui/Tip'
import Typography from '@mui/material/Typography'
import { Link as RouterLink } from 'react-router-dom'
import { get, post } from '../../api'
import { useAuth } from '../../auth'
import { TabPanel } from '../../components/Tabs'
import { personaLabel } from '../../components/tasks/personaRegistry'
import { useApiRead } from '../../hooks/useApiRead'
import { useSubmit } from '../../hooks/useSubmit'
import { AUTH_KIND_LABEL } from '../../lib/authKindLabel'
import { groupedCountOf, humanize } from '../../lib/humanize'
import { planSurfaceShown } from '../../lib/planSwitches'
import { NO_VALUE } from '../../lib/relativeTime'
import { safeHref } from '../../lib/safeHref'
import { FONT_WEIGHT, LINE_HEIGHT, RADIUS, grid } from '../../theme'
import type { AccountAuthKind, PipelineTask } from '../../types'
import RelativeTime from '../../ui/RelativeTime'
import { DetailField, DetailSection } from '../../ui/detail/DetailPage'
import { Mono } from '../../ui/DetailFields'
import { FactsGrid } from '../../ui/FactsGrid'
import { StatusChip, type Tone } from '../../ui/StatusChip'
import { isEnabled } from '../../lib/editionFeatures'
import type { BenchmarkDuel, OutcomeLink, OutcomeRow, TaskOutcomesResponse } from '../../components/outcomes/types'
import type { DetailsSubtab } from './taskDetailConstants'

/** The task's ledger read: one per page, shared by the Details strip (does the subtab exist) and the panel. Keyed on the
 *  task's `updated_at` and state, so a settle or a judged link (the `outcome_judged` console event refreshes the task)
 *  re-reads it. Not on the Live Cast viewer: the public API serves no ledger. Nor off the plan's agent performance (a
 *  tenant's 403): no read, so no subtab. Nor without the Command center (`exec_dashboard`), whose API the read is. */
export function useTaskOutcomes(task: PipelineTask | null, enabled: boolean) {
  const shown = planSurfaceShown(useAuth().admin, 'agent_performance')
  return useApiRead<TaskOutcomesResponse>(
    ['task-outcomes', task?.id, task?.updated_at, task?.state],
    () => get<TaskOutcomesResponse>(`/api/tasks/${task?.id ?? 0}/outcomes`),
    'Failed to load the outcome',
    { enabled: enabled && shown && task !== null && isEnabled('exec_dashboard') },
  )
}

export type TaskOutcomesRead = ReturnType<typeof useTaskOutcomes>

// ── Vocabulary (the API's closed sets: the outcome ledger model's vocabularies) ─────────────────────────────────────────

const STATE: Record<string, { label: string; tone: Tone; help: string }> = {
  pending: { label: 'Pending', tone: 'info', help: 'Not merged yet' },
  merged: { label: 'Merged', tone: 'info', help: 'Merged; its survival window is still open' },
  deployed: { label: 'Released', tone: 'info', help: 'Merged and released; its survival window is still open' },
  survived: { label: 'Survived', tone: 'success', help: 'Merged and lasted its survival window with no defect' },
  reverted: { label: 'Reverted', tone: 'error', help: 'A later commit reverted this merge' },
  fixed_forward: { label: 'Fixed forward', tone: 'warning', help: 'A later change fixed a defect this merge caused' },
  incident_linked: { label: 'Incident linked', tone: 'error', help: 'A production incident was traced to this merge' },
  failed: { label: 'Failed', tone: 'error', help: 'The attempt failed without merging' },
  abandoned: { label: 'Abandoned', tone: 'neutral', help: 'The attempt ended without merging' },
  settled: { label: 'Settled', tone: 'success', help: 'Finished with nothing to merge' },
}

const LINK_KIND: Record<string, string> = {
  revert: 'Revert',
  fixes_ref: 'Fix reference',
  same_files: 'Same files',
  caused_by: 'Caused by',
  incident: 'Incident',
}

const HOSTING: Record<string, string> = { jaah_cloud: 'Jaah Cloud', self_hosted: 'Self-hosted' }
const ISOLATION: Record<string, string> = { static: 'Static', microvm: 'MicroVM' }

/** A link's confidence as a band: High ≥ 0.8, Medium ≥ 0.5, else Low (Architecture §9.3). */
export function confidenceBand(confidence: number): 'High' | 'Medium' | 'Low' {
  if (confidence >= 0.8) return 'High'
  if (confidence >= 0.5) return 'Medium'
  return 'Low'
}

const BAND_TONE: Record<ReturnType<typeof confidenceBand>, Tone> = { High: 'error', Medium: 'warning', Low: 'neutral' }

/** The node a dispatch ran on, as one word pair ("Jaah Cloud · MicroVM"); null when neither is known. */
export function nodeClass(row: Pick<OutcomeRow, 'node_hosting' | 'node_isolation'>): string | null {
  const parts = [
    row.node_hosting ? HOSTING[row.node_hosting] ?? humanize(row.node_hosting) : null,
    row.node_isolation ? ISOLATION[row.node_isolation] ?? humanize(row.node_isolation) : null,
  ].filter((p): p is string => p !== null)
  return parts.length > 0 ? parts.join(' · ') : null
}

/** A commit's page on the forge, from the PR's URL (GitHub/Gitea `/pull/N`, GitLab `/-/merge_requests/N`, Bitbucket
 *  `/pull-requests/N`); null when the PR URL names no known shape. */
export function commitHref(prUrl: string | null, sha: string): string | undefined {
  const safe = safeHref(prUrl)
  if (!safe) return undefined
  const gitlab = safe.match(/^(.*)\/-\/merge_requests\/\d+/)
  if (gitlab) return `${gitlab[1]}/-/commit/${sha}`
  const bitbucket = safe.match(/^(.*)\/pull-requests\/\d+/)
  if (bitbucket) return `${bitbucket[1]}/commits/${sha}`
  const github = safe.match(/^(.*)\/pulls?\/\d+/)
  if (github) return `${github[1]}/commit/${sha}`
  return undefined
}

// ── Pieces ──────────────────────────────────────────────────────────────────────────────────────────────────────

/** One attribution fact as a chip that wraps rather than truncates; an unknown value reads "—". */
function AttributionChip({ name, value, testId }: { name: string; value: string | null; testId: string }) {
  return (
    <Chip
      size="small"
      variant="outlined"
      data-testid={testId}
      label={<><Box component="span" sx={{ color: 'text.secondary' }}>{name}</Box> {value ?? NO_VALUE}</>}
      sx={{ height: 'auto', maxWidth: '100%', '& .MuiChip-label': { whiteSpace: 'normal', overflowWrap: 'anywhere', py: 0 } }}
    />
  )
}

function Stamp({ iso }: { iso: string | null }) {
  return iso ? <RelativeTime iso={iso} absolute /> : <Typography component="span" sx={{ color: 'text.secondary' }}>{NO_VALUE}</Typography>
}

const num = (value: number | null, unit = '') => (value === null ? NO_VALUE : `${value.toLocaleString()}${unit}`)
const minutes = (value: number | null) => (value === null ? NO_VALUE : `${Math.round(value * 10) / 10} min`)

const filesText = (n: number | null) => (n === null ? NO_VALUE : groupedCountOf(n, 'file'))

function LinkEvidence({ link, prUrl }: { link: OutcomeLink; prUrl: string | null }) {
  const title = link.evidence_title
  const incident = /^incident:(\d+)$/.exec(link.evidence_ref)
  if (link.kind === 'incident' && incident) {
    return <Link component={RouterLink} to={`/incidents/${incident[1]}`}>{title ?? `Incident #${incident[1]}`}</Link>
  }
  if (link.evidence_kind === 'task' && /^\d+$/.test(link.evidence_ref)) {
    return <Link component={RouterLink} to={`/tasks/${link.evidence_ref}`}>{title ?? `Task #${link.evidence_ref}`}</Link>
  }
  const href = link.evidence_kind === 'commit' ? commitHref(prUrl, link.evidence_ref) : safeHref(link.evidence_ref)
  const text = link.evidence_kind === 'commit'
    ? <>{title ? `${title} ` : ''}<Mono>{link.evidence_ref.slice(0, 12)}</Mono></>
    : title ?? link.evidence_ref
  return href
    ? <ExternalLink href={href} underline="hover" sx={{ overflowWrap: 'anywhere' }}>{text}</ExternalLink>
    : <Box component="span" sx={{ overflowWrap: 'anywhere' }}>{text}</Box>
}

function linkStateText(link: OutcomeLink): string {
  if (link.state === 'proposed') return 'Proposed'
  const verb = link.state === 'confirmed' ? 'Confirmed' : link.state === 'dismissed' ? 'Dismissed' : humanize(link.state)
  return link.decided_by_name ? `${verb} by ${link.decided_by_name}` : verb
}

function LinkRow({ link, prUrl, canDecide, onDecided }: {
  link: OutcomeLink
  prUrl: string | null
  canDecide: boolean
  onDecided: () => Promise<void>
}) {
  const { busy, formError, run } = useSubmit()
  const band = confidenceBand(link.confidence)
  const decide = (verb: 'confirm' | 'dismiss') => run(
    verb === 'confirm' ? 'Failed to confirm the link' : 'Failed to dismiss the link',
    async () => { await post(`/api/outcome-links/${link.id}/${verb}`); await onDecided() },
  )
  return (
    <Box component="li" data-testid="outcome-link" data-state={link.state}
      sx={{ py: 1, borderTop: 1, borderColor: 'divider', listStyle: 'none', minWidth: 0 }}>
      <Stack direction="row" sx={{ flexWrap: 'wrap', alignItems: 'center', columnGap: 1, rowGap: 0.5 }}>
        <Typography variant="body2" sx={{ fontWeight: FONT_WEIGHT.semibold }}>{LINK_KIND[link.kind] ?? humanize(link.kind)}</Typography>
        <Tip describeChild={false} title={`Confidence ${link.confidence.toFixed(2)}`}>
          <span data-testid="outcome-link-band">
            <StatusChip label={band} tone={link.state === 'dismissed' ? 'neutral' : BAND_TONE[band]}
              ariaLabel={`${band} confidence (${link.confidence.toFixed(2)})`} />
          </span>
        </Tip>
        <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="outcome-link-state">
          {linkStateText(link)}
        </Typography>
      </Stack>
      <Typography variant="body2" component="div" sx={{ mt: 0.5, minWidth: 0 }}>
        <Box component="span" sx={{ color: 'text.secondary' }}>
          {STATE[link.defect_state]?.label ?? humanize(link.defect_state)} ·{' '}
        </Box>
        <LinkEvidence link={link} prUrl={prUrl} />
      </Typography>
      {canDecide && (
        <Stack direction="row" sx={{ gap: 1, mt: 0.5, flexWrap: 'wrap' }}>
          {link.state !== 'confirmed' && (
            <Button size="small" variant="outlined" disabled={busy} onClick={() => void decide('confirm')}
              data-testid="outcome-link-confirm">Confirm</Button>
          )}
          {link.state !== 'dismissed' && (
            <Button size="small" variant="outlined" color="inherit" disabled={busy} onClick={() => void decide('dismiss')}
              data-testid="outcome-link-dismiss">Dismiss</Button>
          )}
        </Stack>
      )}
      {formError && <Typography variant="body2" color="error" sx={{ mt: 0.5 }}>{formError}</Typography>}
    </Box>
  )
}

function DispatchCard({ row, canDecide, onDecided }: { row: OutcomeRow; canDecide: boolean; onDecided: () => Promise<void> }) {
  const state = STATE[row.outcome_state] ?? { label: humanize(row.outcome_state), tone: 'neutral' as Tone, help: '' }
  const tier = row.review_tier === 'unknown' ? null : humanize(row.review_tier)
  const timeline: [string, string | null][] = [
    ['Ready', row.task_ready_at],
    ['Dispatched', row.dispatched_at],
    ['First active', row.first_active_at],
    ['PR opened', row.pr_opened_at],
    ['Enqueued', row.enqueued_at],
    ['Merged', row.merged_at],
    ['Released', row.deployed_at],
    ['Ended', row.ended_at],
    ['Survival ends', row.survival_ends_at],
  ]
  return (
    <Box component="section" data-testid="outcome-dispatch" aria-label={`Dispatch ${row.dispatch_seq}`}
      sx={{ border: 1, borderColor: 'divider', borderRadius: RADIUS.md, p: 1.5, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 1.5 }}>
      <Stack direction="row" sx={{ flexWrap: 'wrap', alignItems: 'center', columnGap: 1, rowGap: 0.5 }}>
        <Typography variant="subtitle2" component="h4">Dispatch {row.dispatch_seq}</Typography>
        <span data-testid="outcome-state"><StatusChip label={state.label} tone={state.tone} title={state.help} /></span>
        {row.backfilled && (
          <StatusChip label="Backfilled" tone="neutral" dashed testId="outcome-backfilled"
            title="Recorded from history after release: merge, lane and cost facts only" />
        )}
        {row.shadow && (
          <StatusChip label="Shadow" tone="info" dashed testId="outcome-shadow"
            title="A benchmark run beside the real one; never merged" />
        )}
        {row.excluded && (
          <StatusChip label="Excluded" tone="neutral" testId="outcome-excluded"
            title={`Left out of every metric${row.excluded_reason ? ` (${row.excluded_reason})` : ''}`} />
        )}
      </Stack>

      <Stack direction="row" data-testid="outcome-attribution" sx={{ flexWrap: 'wrap', gap: 0.5 }}>
        <AttributionChip name="Agent" value={row.agent_label} testId="outcome-chip-agent" />
        <AttributionChip name="Auth" value={row.auth_kind ? (row.auth_kind in AUTH_KIND_LABEL ? AUTH_KIND_LABEL[row.auth_kind as AccountAuthKind] : humanize(row.auth_kind)) : null}
          testId="outcome-chip-auth" />
        <AttributionChip name="Persona" value={personaLabel(row.persona)} testId="outcome-chip-persona" />
        <AttributionChip name="Review" value={tier} testId="outcome-chip-review" />
        <AttributionChip name="Node" value={nodeClass(row)} testId="outcome-chip-node" />
      </Stack>

      <Box>
        <Typography variant="overline" component="h5" sx={{ color: 'text.secondary', lineHeight: LINE_HEIGHT.normal }}>Timeline</Typography>
        <FactsGrid minTile={grid(34.5)} testId="outcome-timeline">
          {timeline.map(([label, iso]) => (
            <DetailField key={label} label={label} dense inline><Stamp iso={iso} /></DetailField>
          ))}
        </FactsGrid>
      </Box>

      <Box>
        <Typography variant="overline" component="h5" sx={{ color: 'text.secondary', lineHeight: LINE_HEIGHT.normal }}>Gate</Typography>
        <FactsGrid minTile={grid(34.5)} testId="outcome-facts">
          <DetailField label="Lane failures" dense inline>{num(row.lane_failures)}</DetailField>
          <DetailField label="Pushes after enqueue" dense inline>{num(row.pushes_after_enqueue)}</DetailField>
          <DetailField label="Non-runner commits" dense inline>{num(row.nonrunner_commits)}</DetailField>
          <DetailField label="Security findings" dense inline>{num(row.security_findings)}</DetailField>
          <DetailField label="Lane time" dense inline>{minutes(row.lane_minutes)}</DetailField>
          <DetailField label="Node time" dense inline>{minutes(row.node_minutes)}</DetailField>
          <DetailField label="Diff" dense inline>
            {row.additions === null && row.deletions === null
              ? NO_VALUE
              : `+${num(row.additions)} −${num(row.deletions)} · ${filesText(row.files_changed)}`}
          </DetailField>
          <DetailField label="Merge commit" dense inline>{row.merge_sha ? <Mono>{row.merge_sha.slice(0, 12)}</Mono> : NO_VALUE}</DetailField>
        </FactsGrid>
      </Box>

      {row.links.length > 0 && (
        <Box>
          <Typography variant="overline" component="h5" sx={{ color: 'text.secondary', lineHeight: LINE_HEIGHT.normal }}>Defect links</Typography>
          <Box component="ul" sx={{ m: 0, p: 0 }} data-testid="outcome-links">
            {row.links.map(link => (
              <LinkRow key={link.id} link={link} prUrl={row.pr_url} canDecide={canDecide} onDecided={onDecided} />
            ))}
          </Box>
        </Box>
      )}
    </Box>
  )
}

const DUEL_STATE: Record<BenchmarkDuel['state'], { label: string; tone: Tone }> = {
  running: { label: 'Running', tone: 'info' },
  reviewing: { label: 'Reviewing', tone: 'info' },
  decided: { label: 'Decided', tone: 'success' },
  void: { label: 'Void', tone: 'neutral' },
}
const GATE: Record<string, string> = { pass: 'Passed', fail: 'Failed', unknown: 'Unknown' }

/** Who won, in the configurations' own names; null until decided. */
export function duelResult(duel: Pick<BenchmarkDuel, 'result' | 'origin_label' | 'shadow_label'>): string | null {
  if (duel.result === 'origin') return `${duel.origin_label} won`
  if (duel.result === 'shadow') return `${duel.shadow_label} won`
  if (duel.result === 'tie') return 'Tie'
  return null
}

const taskLink = (id: number) => (
  <Link component={RouterLink} to={`/tasks/${id}?tab=details&details=outcome`}>Task #{id}</Link>
)

/** The benchmark duel this task is in (benchmark mode): on a sampled origin, its shadow, the shadow's PR and the
 *  result; on the shadow itself, a Shadow badge and its origin. */
export function DuelCard({ duel }: { duel: BenchmarkDuel }) {
  const state = DUEL_STATE[duel.state] ?? { label: humanize(duel.state), tone: 'neutral' as Tone }
  const result = duelResult(duel)
  const prHref = safeHref(duel.shadow_pr_url)
  const prNumber = duel.shadow_pr_url?.match(/(\d+)\/?$/)?.[1]
  return (
    <Box data-testid="outcome-duel" sx={{ border: 1, borderColor: 'divider', borderRadius: RADIUS.sm, p: 1.5, minWidth: 0 }}>
      <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 1, mb: 1 }}>
        <Typography variant="subtitle2" component="h4">Benchmark duel</Typography>
        {duel.role === 'shadow' && <StatusChip label="Shadow" tone="info" testId="outcome-duel-shadow-badge"
          title="A hidden re-run of another task on a different configuration. Its PR is never merged." />}
        <StatusChip label={state.label} tone={state.tone} testId="outcome-duel-state" />
      </Box>
      <FactsGrid minTile={grid(34.5)}>
        <DetailField label={duel.role === 'shadow' ? 'Origin' : 'Shadow'} testId="outcome-duel-other" dense inline>
          {duel.role === 'shadow' ? taskLink(duel.origin_task_id) : duel.shadow_task_id !== null ? taskLink(duel.shadow_task_id) : NO_VALUE}
        </DetailField>
        <DetailField label="Origin config" dense inline>{duel.origin_label}</DetailField>
        <DetailField label="Shadow config" dense inline>{duel.shadow_label}</DetailField>
        <DetailField label="Shadow PR" testId="outcome-duel-pr" dense inline>
          {prHref ? (
            <>
              <ExternalLink href={prHref}>{prNumber ? `#${prNumber}` : 'PR'}</ExternalLink>
              {' '}{duel.shadow_pr_closed ? 'Closed, not merged' : 'Open draft'}
            </>
          ) : NO_VALUE}
        </DetailField>
        <DetailField label="Gate" dense inline>
          {`Origin ${GATE[duel.gate_origin ?? ''] ?? NO_VALUE} · Shadow ${GATE[duel.gate_shadow ?? ''] ?? NO_VALUE}`}
        </DetailField>
        <DetailField label="Diff distance" dense inline>{duel.diff_distance === null ? NO_VALUE : duel.diff_distance.toFixed(2)}</DetailField>
        <DetailField label="Result" testId="outcome-duel-result" dense inline>
          {result ?? (duel.state === 'void' ? `Void${duel.void_reason ? `: ${duel.void_reason}` : ''}` : 'Pending')}
        </DetailField>
      </FactsGrid>
      {duel.review_reason && (
        <Typography variant="body2" sx={{ mt: 1, color: 'text.secondary', overflowWrap: 'anywhere' }} data-testid="outcome-duel-reason">
          Review: {duel.review_reason}
        </Typography>
      )}
    </Box>
  )
}

export function TaskOutcomePanel({ current, read }: { current: DetailsSubtab; read: TaskOutcomesRead }) {
  const shown = planSurfaceShown(useAuth().admin, 'agent_performance')
  const data = read.data
  const canDecide = data?.can_decide === true
  if (!shown) return null
  return (
    <TabPanel id="outcome" current={current} idPrefix="details">
      {/* Caused by is a field on the Task tab (`components/tasks/TaskPageFields`), riding its Save. */}
      <DetailSection testId="task-page-panel" title="Outcome" headingLevel="h3">
        {read.error && <Typography color="error" variant="body2">{read.error}</Typography>}
        <Stack sx={{ gap: 2, minWidth: 0 }}>
          {data?.duel && <DuelCard duel={data.duel} />}
          {(data?.rows ?? []).map(row => (
            <DispatchCard key={row.id} row={row} canDecide={canDecide} onDecided={read.refresh} />
          ))}
        </Stack>
      </DetailSection>
    </TabPanel>
  )
}

export default TaskOutcomePanel
