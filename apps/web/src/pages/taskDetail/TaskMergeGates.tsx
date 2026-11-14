import type { JSX } from 'react'
import { useQuery } from '@tanstack/react-query'
import { GateRunTable } from '../../components/pipeline/gates/GateResults'
import { gateRunsKey, getGateRuns } from '../../lib/pipelineGates'
import { DetailSection } from '../../ui/detail/DetailPage'

/* The Task page's merge gates (PRD D15 a): the newest merge lane gate run whose batch carried this task's pull
 * request, a row per gate (`GET /api/projects/{id}/gate-runs?task_id=`). Draws nothing until a batch ran. */
export function TaskMergeGates({ projectId, taskId }: { projectId: number; taskId: number }): JSX.Element | null {
  const query = { taskId }
  const runs = useQuery({
    queryKey: gateRunsKey(projectId, query),
    queryFn: () => getGateRuns(projectId, query),
    refetchInterval: q => ((q.state.data?.runs ?? []).some(r => r.run.state === 'running') ? 10_000 : false),
  })
  const run = (runs.data?.runs ?? []).find(r => r.results.length > 0)
  if (!run) return null
  return (
    <DetailSection testId="task-merge-gates" title="Merge gates" headingLevel="h3">
      <GateRunTable run={run} title="Merge gates" hideTitle testId="task-merge-gate-results" />
    </DetailSection>
  )
}
