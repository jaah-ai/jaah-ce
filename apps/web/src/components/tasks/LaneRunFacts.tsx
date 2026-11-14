/* A MERGE-LANE RUN'S OWN NUMBERS — `9 gates · 72m` and, past the stall bound, a Slow mark — drawn beside the
 * task's name on the board and the task page. Never cut: the line is short by design and does not shrink. */
import Box from '@mui/material/Box'
import { StatusChip } from '../../ui/StatusChip'
import { monoSx } from '../../ui/listStyles'
import Tip from '../../ui/Tip'
import { laneRunLabel, type LaneRunStats } from './laneRun'

const FACTS_SX = { display: 'inline-flex', alignItems: 'center', gap: 1, flex: '0 0 auto', whiteSpace: 'nowrap' } as const

export default function LaneRunFacts({ run }: { run: LaneRunStats | null | undefined }) {
  if (!run) return null
  const label = laneRunLabel(run)
  return (
    <Box component="span" data-testid="lane-run" sx={FACTS_SX}>
      <Tip title="Gates this run ran · its wall time"><Box
        component="span"
        data-testid="lane-run-facts"
        sx={theme => ({ ...monoSx(theme), color: 'text.secondary' })}
      >
        {label}
      </Box></Tip>
      {run.slow && (
        <StatusChip label="Slow" tone="warning" title="Past twice the last full gate" testId="lane-run-slow" />
      )}
    </Box>
  )
}
