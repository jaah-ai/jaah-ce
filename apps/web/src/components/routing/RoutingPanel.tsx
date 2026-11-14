// The task page's Routing tab: what the agent router decided for this task's
// next dispatch, and for every dispatch before it, and the review lanes its runs reported. Reads `GET /api/tasks/{id}/routing`
// (`apps/api/app/routes/routing.py`), which renders every name and reason in words — this file maps only the
// closed status sets to labels. Reasons wrap and are never truncated. Two actions: "Use this" accepts a live
// Recommend pick (`POST …/routing/accept`), and "Change" on an Auto pick hands over to the task's own edit form.
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Link from '@mui/material/Link'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import type { JSX } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Link as RouterLink } from 'react-router-dom'
import { get, post } from '../../api'
import { useApiRead } from '../../hooks/useApiRead'
import { useSubmit } from '../../hooks/useSubmit'
import { FONT_WEIGHT, LINE_HEIGHT, RADIUS, grid } from '../../theme'
import RelativeTime from '../../ui/RelativeTime'
import { DetailField } from '../../ui/detail/DetailPage'
import { EmptyState } from '../../ui/EmptyState'
import { FactsGrid } from '../../ui/FactsGrid'
import { StatusChip, type Tone } from '../../ui/StatusChip'
import { statusForState } from '../tasks/shared'

export interface RoutingConfig {
  runner: string
  tier: string | null
  model: string | null
  review: string
}

export interface RoutingCandidate {
  config: RoutingConfig
  eligible: boolean
  excluded_reason: string | null
  score: number | null
  sample: number | null
}

export interface RoutingDecision {
  id: string
  attempt: number
  status: 'pending' | 'locked' | 'final'
  mode: 'shadow' | 'recommend' | 'auto'
  mode_reason: string
  reason: string
  used: 'router' | 'default'
  router_pick: RoutingConfig
  default_pick: RoutingConfig
  actual: RoutingConfig | null
  candidates: RoutingCandidate[]
  recommend_response: 'accepted' | 'overridden' | 'no_response' | null
  human_pick: RoutingConfig | null
  exploratory: boolean
  /** A person may still accept this recommendation ("Use this"): pending, Recommend, unanswered. */
  can_accept: boolean
  created_at: string
  locked_at: string | null
  finalized_at: string | null
}

export interface ReviewLaneUsed {
  lane: string
  runner: string
  model: string | null
  reported_at: string
}

export interface TaskRouting {
  task_id: number
  live: RoutingDecision | null
  history: RoutingDecision[]
  review_lanes_used?: ReviewLaneUsed[]
  /** A coder task's Blind review line: which review subtasks were filed, or why none were. */
  blind_review?: string | null
  /** Those subtasks, each with the runner reviewing it (null before the router decided). */
  blind_children?: BlindChild[]
}

export interface BlindChild {
  task_id: number
  role: string
  state: string
  runner: string | null
}

/** The coder's blind review subtasks, one line each: role (a link), status, and the runner reviewing it. */
function BlindReview({ line, items }: { line: string | null | undefined; items: BlindChild[] }): JSX.Element | null {
  if (!line && items.length === 0) return null
  return (
    <Stack sx={{ gap: 0.5, minWidth: 0 }} data-testid="routing-blind-review">
      {line && <Typography variant="body2">{line}</Typography>}
      {items.map(child => (
        <Typography key={child.task_id} variant="body2" data-testid="routing-blind-child" sx={{ overflowWrap: 'anywhere' }}>
          <Link component={RouterLink} to={`/tasks/${child.task_id}`}>{child.role}</Link>
          {` · ${statusForState(child.state).label} · ${child.runner ?? 'Runner not chosen yet'}`}
        </Typography>
      ))}
    </Stack>
  )
}

const STATUS: Record<RoutingDecision['status'], { label: string; tone: Tone }> = {
  pending: { label: 'Next dispatch', tone: 'info' },
  locked: { label: 'Dispatching', tone: 'warning' },
  final: { label: 'Dispatched', tone: 'success' },
}
const RESPONSE: Record<NonNullable<RoutingDecision['recommend_response']>, string> = {
  accepted: 'Accepted',
  overridden: 'Overridden',
  no_response: 'No response',
}

/** A configuration in words: "Acme Runner · High · acme-large-2"; tier and model only when set. */
export function configText(cfg: RoutingConfig): string {
  return [cfg.runner, cfg.tier, cfg.model].filter((part): part is string => !!part).join(' · ')
}

export const WRAP = { overflowWrap: 'anywhere', whiteSpace: 'normal', minWidth: 0 } as const

export function Fact({ label, value, testId }: { label: string; value: string; testId?: string }) {
  return <DetailField label={label} testId={testId} dense inline>{value}</DetailField>
}

function Candidates({ candidates }: { candidates: RoutingCandidate[] }) {
  return (
    <Box component="ul" data-testid="routing-candidates" sx={{ m: 0, p: 0, listStyle: 'none' }}>
      {candidates.map((c, i) => (
        <Box component="li" key={i} data-testid="routing-candidate"
          sx={{ py: 0.5, borderTop: 1, borderColor: 'divider', minWidth: 0 }}>
          <Stack direction="row" sx={{ flexWrap: 'wrap', alignItems: 'center', columnGap: 1, rowGap: 0.5 }}>
            <Typography variant="body2" sx={{ fontWeight: FONT_WEIGHT.semibold, ...WRAP }}>{configText(c.config)}</Typography>
            <StatusChip label={c.eligible ? 'Eligible' : 'Excluded'} tone={c.eligible ? 'success' : 'neutral'} />
            <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="routing-candidate-score">
              {c.score === null
                ? 'Insufficient data'
                : `Score ${c.score.toFixed(2)}${c.sample !== null ? ` · ${c.sample.toLocaleString()} runs` : ''}`}
            </Typography>
          </Stack>
          {!c.eligible && c.excluded_reason && (
            <Typography variant="body2" sx={{ color: 'text.secondary', mt: 0, ...WRAP }}>{c.excluded_reason}</Typography>
          )}
        </Box>
      ))}
    </Box>
  )
}

export function DecisionCard({ decision, onAccept, accepting = false, onChange }: {
  decision: RoutingDecision
  /** "Use this", offered while `decision.can_accept`. */
  onAccept?: () => void
  accepting?: boolean
  /** "Change" on a pending Auto pick: the host opens where the task's runner is edited. */
  onChange?: () => void
}): JSX.Element {
  const status = STATUS[decision.status]
  const changeable = decision.mode === 'auto' && decision.status === 'pending' && onChange !== undefined
  const routerDiffers = configText(decision.router_pick) !== configText(decision.default_pick)
  const stamp = decision.finalized_at ?? decision.locked_at ?? decision.created_at
  return (
    <Box component="section" data-testid="routing-decision" data-status={decision.status}
      aria-label={`Attempt ${decision.attempt}`}
      sx={{ border: 1, borderColor: 'divider', borderRadius: RADIUS.md, p: 1.5, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 1.5 }}>
      <Stack direction="row" sx={{ flexWrap: 'wrap', alignItems: 'center', columnGap: 1, rowGap: 0.5 }}>
        <Typography variant="subtitle2" component="h4">Attempt {decision.attempt}</Typography>
        <StatusChip label={status.label} tone={status.tone} testId="routing-status" />
        {decision.exploratory && <StatusChip label="Exploring" tone="info" />}
        <Typography variant="body2" component="span" sx={{ color: 'text.secondary' }}><RelativeTime iso={stamp} absolute /></Typography>
      </Stack>

      <Typography variant="body1" data-testid="routing-reason" sx={WRAP}>{decision.reason}</Typography>

      <FactsGrid minTile={grid(42.5)} testId="routing-facts">
        <Fact label="Mode" value={decision.mode_reason} testId="routing-mode-reason" />
        <Fact label="Uses" value={decision.used === 'router' ? "The router's pick" : 'The default'} />
        <Fact label="Router's pick" value={configText(decision.router_pick)} testId="routing-router-pick" />
        <Fact label="Review" value={decision.router_pick.review} testId="routing-review" />
        <Fact label="Default" value={routerDiffers ? configText(decision.default_pick) : "Same as the router's pick"}
          testId="routing-default-pick" />
        <Fact label="Ran on" value={decision.actual ? configText(decision.actual) : decision.status === 'locked' ? 'Starting' : 'Not dispatched yet'}
          testId="routing-actual" />
        {decision.mode === 'recommend' && (
          <Fact label="Recommendation"
            value={decision.recommend_response ? RESPONSE[decision.recommend_response] : 'Awaiting a response'}
            testId="routing-response" />
        )}
        {decision.human_pick && <Fact label="Person's pick" value={configText(decision.human_pick)} testId="routing-human-pick" />}
      </FactsGrid>

      {((decision.can_accept && onAccept) || changeable) && (
        <Stack direction="row" sx={{ flexWrap: 'wrap', gap: 1 }}>
          {decision.can_accept && onAccept && (
            <Button variant="contained" size="small" onClick={onAccept} disabled={accepting} data-testid="routing-accept">
              Use this
            </Button>
          )}
          {changeable && (
            <Button variant="outlined" size="small" onClick={onChange} data-testid="routing-change">Change</Button>
          )}
        </Stack>
      )}

      {decision.candidates.length > 0 && (
        <Box>
          <Typography variant="overline" component="h5" sx={{ color: 'text.secondary', lineHeight: LINE_HEIGHT.normal }}>Candidates</Typography>
          <Candidates candidates={decision.candidates} />
        </Box>
      )}
    </Box>
  )
}

/** The review lanes the task's runs reported running, newest run first: "Code reviewer — Acme Runner · acme-large-2". */
function ReviewLanesUsed({ lanes }: { lanes: ReviewLaneUsed[] }): JSX.Element {
  return (
    <Box component="section" aria-label="Review lanes used" data-testid="routing-review-lanes">
      <Typography variant="overline" component="h4" sx={{ color: 'text.secondary', lineHeight: LINE_HEIGHT.normal }}>Review lanes used</Typography>
      <Box component="ul" sx={{ m: 0, p: 0, listStyle: 'none' }}>
        {lanes.map((lane, i) => (
          <Box component="li" key={i} data-testid="routing-review-lane"
            sx={{ py: 0.5, borderTop: 1, borderColor: 'divider', minWidth: 0 }}>
            <Stack direction="row" sx={{ flexWrap: 'wrap', alignItems: 'baseline', columnGap: 1, rowGap: 0 }}>
              <Typography variant="body2" sx={{ fontWeight: FONT_WEIGHT.semibold, ...WRAP }}>{lane.lane}</Typography>
              <Typography variant="body2" sx={{ color: 'text.secondary', ...WRAP }}>
                {[lane.runner, lane.model].filter((part): part is string => !!part).join(' · ')}
              </Typography>
              <Typography variant="body2" component="span" sx={{ color: 'text.secondary' }}>
                <RelativeTime iso={lane.reported_at} absolute />
              </Typography>
            </Stack>
          </Box>
        ))}
      </Box>
    </Box>
  )
}

export function useTaskRouting(taskId: number, enabled: boolean) {
  return useApiRead<TaskRouting>(
    ['task-routing', taskId],
    () => get<TaskRouting>(`/api/tasks/${taskId}/routing`),
    'Failed to load routing',
    { enabled },
  )
}

/** The panel body: the next dispatch's decision, then every earlier one. `active`: read only once the tab shows.
 *  `onAccepted`: the accept wrote the task's runner and model, so the host re-reads its row. `onChange`: Auto's
 *  "Change" — the host opens the task's edit form (an ordinary edit). */
export function RoutingPanel({ taskId, active = true, onAccepted, onChange }: {
  taskId: number
  active?: boolean
  onAccepted?: () => void
  onChange?: () => void
}): JSX.Element {
  const read = useTaskRouting(taskId, active)
  const client = useQueryClient()
  const { busy: accepting, formError: acceptError, run } = useSubmit()
  const accept = async () => {
    const landed = await run('Failed to accept the recommendation', async () => {
      client.setQueryData(['task-routing', taskId], await post<TaskRouting>(`/api/tasks/${taskId}/routing/accept`))
      onAccepted?.()
    })
    if (!landed) void read.refresh()
  }
  const data = read.data
  if (read.error) return <Typography color="error" variant="body2">{read.error}</Typography>
  if (!data) return <Box data-testid="routing-loading" />
  const lanes = data.review_lanes_used ?? []
  const blind = <BlindReview line={data.blind_review} items={data.blind_children ?? []} />
  if (!data.live && data.history.length === 0 && lanes.length === 0) {
    return data.blind_review || data.blind_children?.length ? blind : <EmptyState title="No routing decision yet" />
  }
  return (
    <Stack sx={{ gap: 2, minWidth: 0 }} data-testid="routing-panel">
      {blind}
      {acceptError && <Typography color="error" variant="body2" role="alert">{acceptError}</Typography>}
      {data.live && (
        <DecisionCard decision={data.live} onAccept={() => void accept()} accepting={accepting} onChange={onChange} />
      )}
      {lanes.length > 0 && <ReviewLanesUsed lanes={lanes} />}
      {data.history.length > 0 && (
        <Stack sx={{ gap: 1.5, minWidth: 0 }} data-testid="routing-history">
          <Typography variant="overline" component="h4" sx={{ color: 'text.secondary', lineHeight: LINE_HEIGHT.normal }}>
            {data.live ? 'Earlier dispatches' : 'Dispatches'}
          </Typography>
          {data.history.map(decision => <DecisionCard key={decision.id} decision={decision} />)}
        </Stack>
      )}
    </Stack>
  )
}

export default RoutingPanel
