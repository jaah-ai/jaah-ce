import { type JSX, useCallback } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import { useAuth, useCanOnProject } from '../../auth'
import { usePagedList } from '../../hooks/usePagedList'
import { useProjects } from '../../hooks/useRosters'
import { useUrlFilters } from '../../hooks/useUrlFilters'
import { planSwitchOn } from '../../lib/planSwitches'
import { SIZE } from '../../theme'
import Banner from '../../ui/Banner'
import EmptyState from '../../ui/EmptyState'
import { FilterSelect } from '../../ui/FilterRow'
import LoadingState from '../../ui/LoadingState'
import { LoadMoreFooter } from '../../ui/PagedList'
import ProjectMemoryTab, { LIST_SX, OffPlanNotice, UnitRow, useUnitActions } from '../../components/projects/ProjectMemoryTab'
import {
  listOrgUnits,
  memoryFeedsKey,
  type OrgMemoryUnit,
} from '../../components/projects/projectMemory'

/* AUDIT › MEMORY (`/audit/memory`): what runners learned, across every project the viewer may see whose org's plan
 * carries project memory, newest first, a page at a time (`GET /api/memory/units`, keyset-paged). Each row is the
 * project tab's own (`UnitRow`): Body, and with `projects:edit` on ITS project Retire, Release and Purge; its project's
 * name filters the list to it. `?project=<id>` opens that project's whole Memory view — feed chips, search, kind,
 * status, sort and repository filters (`components/projects/ProjectMemoryTab.tsx`); a project's old `?tab=memory`
 * lands there. Off the viewer's plan the org-wide list says so in one calm line; a project picked off the plan still
 * offers its editor Purge by `mem-N` (erasure is never plan-gated). */

export default function MemoryTab(): JSX.Element {
  const [params, setFilter] = useUrlFilters()
  const projectParam = Number(params.get('project'))
  const projectId = Number.isInteger(projectParam) && projectParam > 0 ? projectParam : null
  const { admin } = useAuth()
  const projects = useProjects()
  const canOnProject = useCanOnProject()
  const queryClient = useQueryClient()
  const project = projectId === null ? undefined : projects.data?.find(p => p.id === projectId)
  /* The operator reads every org's projects, each on its own plan: the server answers per org. */
  const planOn = admin?.is_operator === true || planSwitchOn(admin, 'project_memory_enabled')
  /* The operator's off-plan line links the project's org's Overrides tab (`OffPlanNotice`). */
  const orgId = admin?.is_operator === true ? project?.org?.id ?? admin.org?.id ?? null : null

  return (
    <Box data-testid="audit-memory" sx={{ pt: 1, minWidth: 0 }}>
      <Box data-testid="memory-project-filter" sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, mb: 1.5 }}>
        <FilterSelect label="Project" ariaLabel="Filter by project" allLabel="All projects" grow maxWidth={SIZE.measureNarrow}
          disabled={projects.isPending}
          value={projectId === null ? '' : String(projectId)} onChange={v => {
            // A project's off-plan answer is kept for its open only (`OFF_PLAN_ONCE`): leaving it asks again next time.
            if (projectId !== null) queryClient.removeQueries({ queryKey: memoryFeedsKey(projectId) })
            setFilter({ project: v })
          }}
          options={(projects.data ?? []).map(p => ({ value: String(p.id), label: p.name }))} />
      </Box>
      {projectId !== null
        ? <ProjectMemoryTab key={projectId} projectId={projectId} canEdit={canOnProject('projects:edit', projectId)}
            orgId={orgId}
            repos={[...(project?.repos ?? [])].sort((a, b) => Number(b.is_primary) - Number(a.is_primary)).map(r => r.name)} />
        : planOn ? <OrgUnits onProject={id => setFilter({ project: String(id) })} />
          : <OffPlanNotice orgId={null} allProjects />}
    </Box>
  )
}

function OrgUnits({ onProject }: { onProject: (id: number) => void }): JSX.Element {
  const canOnProject = useCanOnProject()
  const fetchPage = useCallback((cursor: string | null) => listOrgUnits(null, cursor), [])
  const units = usePagedList({
    fetchPage, rowsOf: p => p.items, nextOf: p => p.next_cursor, errorFallback: 'Could not load memory units',
  })
  const { busy, error, notice, confirmDialog, retire, release, purge } = useUnitActions<OrgMemoryUnit>({
    projectIdOf: unit => unit.project.id,
    refresh: () => units.refresh(),
    identitySuffix: unit => ` (${unit.project.name})`,
  })

  if (!units.loaded && !units.error) return <LoadingState rows={3} />
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, minWidth: 0 }}>
      {confirmDialog}
      {error && <Banner severity="error">{error}</Banner>}
      {notice && <Banner severity="success">{notice}</Banner>}
      {units.error && <Banner severity="error">{units.error}</Banner>}
      {units.loaded && units.rows.length === 0 ? <EmptyState title="No memory yet" /> : (
        <Box ref={units.bodyRef}>
          <Box component="ul" aria-label="Memory units" data-testid="memory-units-list" sx={LIST_SX}>
            {units.rows.map(unit => (
              <UnitRow
                key={unit.id}
                unit={unit}
                project={{ name: unit.project.name, onPick: () => onProject(unit.project.id) }}
                canEdit={canOnProject('projects:edit', unit.project.id)}
                busy={busy.has(unit.id)}
                onRetire={() => void retire(unit)}
                onRelease={() => void release(unit)}
                onPurge={() => void purge(unit)}
                renderChunk={() => <></>}
              />
            ))}
          </Box>
          <LoadMoreFooter hasMore={units.hasMore} loading={units.loading} onMore={() => void units.loadMore()}
            testId="memory-more" sx={{ mt: 1.5 }} />
        </Box>
      )}
    </Box>
  )
}
