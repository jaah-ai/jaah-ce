import type { JSX } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../../auth'
import { planSurfaceShown } from '../../lib/planSwitches'
import { INCIDENT_STATE, incidentsForTask, wordOf } from '../../lib/productionQa'
import StatusChip from '../../ui/StatusChip'

/* The task page's "Incident" chip: drawn when the task was filed for an incident (an `incident_tasks` row,
 * `GET /api/incidents?task_id=`), and opens that incident. Nothing while unknown or when there is none, and neither
 * drawn nor read while the org's plan leaves Production QA out (the operator always). */
export default function TaskIncidentChip({ taskId }: { taskId: number }): JSX.Element | null {
  const navigate = useNavigate()
  const shown = planSurfaceShown(useAuth().admin, 'production_qa')
  const read = useQuery({
    queryKey: ['task-incident', taskId],
    queryFn: () => incidentsForTask(taskId),
    enabled: shown,
    staleTime: 60_000,
    retry: false,
    meta: { persist: false },
  })
  // Only a page of incidents draws a chip; any other answer (an error, an empty page) draws nothing.
  const items = read.data?.items
  const incident = Array.isArray(items) ? items[0] : undefined
  if (!shown || !incident) return null
  const state = wordOf(INCIDENT_STATE, incident.state)
  return (
    <span data-testid="task-incident-chip">
      <StatusChip
        label="Incident"
        glyph="⚠"
        tone={state.tone}
        title={`Incident #${incident.id} (${state.label}): ${incident.title}`}
        ariaLabel={`Open incident #${incident.id}`}
        onClick={() => navigate(`/incidents/${incident.id}`)}
        press={{ glyph: '→', label: 'Open' }}
      />
    </span>
  )
}
