// The Task page's merge-lane stage timings: each lane gate run whose tree held one of this task's PRs, newest first,
// with every gate stage's duration and how it ended. Reads `GET /api/tasks/{id}/lane-stages`
// (`apps/api/app/production_qa/lane_timings.py::for_task`); draws nothing until a run covered the task, and neither
// draws nor reads while the org's plan leaves Production QA out (the operator always).
import Box from '@mui/material/Box'
import Link from '@mui/material/Link'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { Link as RouterLink } from 'react-router-dom'
import { get } from '../../api'
import { useAuth } from '../../auth'
import { useApiRead } from '../../hooks/useApiRead'
import { planSurfaceShown } from '../../lib/planSwitches'
import { humanDuration } from '../../lib/relativeTime'
import { RADIUS } from '../../theme'
import RelativeTime from '../../ui/RelativeTime'
import { DetailSection } from '../../ui/detail/DetailPage'
import { StatusChip, type Tone } from '../../ui/StatusChip'

export interface LaneStage { stage: string; seconds: number; outcome: 'passed' | 'failed' | 'flaked' }
export interface LaneRun {
  lane_task_id: number | null
  tree_hash: string | null
  recorded_at: string
  pr_numbers: number[]
  stages: LaneStage[]
}

// A passed stage carries no chip: only what went wrong stands out.
const OUTCOME: Record<'failed' | 'flaked', { label: string; tone: Tone; help?: string }> = {
  failed: { label: 'Failed', tone: 'error' },
  flaked: { label: 'Flaked', tone: 'warning', help: 'Passed on a retry' },
}

function RunCard({ run }: { run: LaneRun }) {
  return (
    <Box component="section" data-testid="lane-stages-run" aria-label="Lane gate run"
      sx={{ border: 1, borderColor: 'divider', borderRadius: RADIUS.md, p: 1.5, minWidth: 0 }}>
      <Stack direction="row" sx={{ flexWrap: 'wrap', alignItems: 'baseline', columnGap: 1, rowGap: 0, mb: 1 }}>
        <RelativeTime iso={run.recorded_at} absolute variant="subtitle2" />
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          {run.pr_numbers.length
            ? `${run.pr_numbers.length === 1 ? 'PR' : 'PRs'} ${run.pr_numbers.map(n => `#${n}`).join(', ')}`
            : 'No PR'}
        </Typography>
        {run.lane_task_id !== null && (
          <Link component={RouterLink} to={`/tasks/${run.lane_task_id}`} variant="body2" underline="hover">
            Lane run #{run.lane_task_id}
          </Link>
        )}
      </Stack>
      <Box component="ul" data-testid="lane-stages-list"
        sx={{ m: 0, p: 0, listStyle: 'none', display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) auto auto',
          columnGap: 1.5, rowGap: 0.5, alignItems: 'center' }}>
        {run.stages.map((s, i) => {
          const outcome = s.outcome === 'passed' ? null : OUTCOME[s.outcome]
          return (
            <Box component="li" key={`${s.stage}-${i}`} sx={{ display: 'contents' }}>
              <Typography variant="body2" sx={{ overflowWrap: 'anywhere' }}>{s.stage}</Typography>
              <Typography variant="body2" sx={{ fontVariantNumeric: 'tabular-nums', textAlign: 'right' }}>
                {humanDuration(s.seconds)}
              </Typography>
              {outcome ? <StatusChip label={outcome.label} tone={outcome.tone} title={outcome.help} /> : <span />}
            </Box>
          )
        })}
      </Box>
    </Box>
  )
}

export function TaskLaneStages({ taskId, updatedAt }: { taskId: number; updatedAt: string }) {
  const shown = planSurfaceShown(useAuth().admin, 'production_qa')
  const { data } = useApiRead<{ runs: LaneRun[] }>(
    ['task-lane-stages', taskId, updatedAt],
    () => get<{ runs: LaneRun[] }>(`/api/tasks/${taskId}/lane-stages`),
    'Failed to load the merge-lane stage timings',
    { enabled: shown },
  )
  if (!shown || !data?.runs?.length) return null
  return (
    <DetailSection testId="task-page-panel" title="Merge-lane stages" headingLevel="h3">
      <Stack spacing={1.5} data-testid="task-lane-stages">
        {data.runs.map(run => <RunCard key={`${run.lane_task_id}-${run.tree_hash}-${run.recorded_at}`} run={run} />)}
      </Stack>
    </DetailSection>
  )
}
