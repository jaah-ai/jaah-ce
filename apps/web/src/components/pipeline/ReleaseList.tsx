import { type JSX, type ReactNode, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import ButtonBase from '@mui/material/ButtonBase'
import { useQuery } from '@tanstack/react-query'
import { apiDetail } from '../../api'
import { useAuth } from '../../auth'
import { planSurfaceShown } from '../../lib/planSwitches'
import { countOf } from '../../lib/humanize'
import { compactAge } from '../../lib/relativeTime'
import { RELEASE_POLL_MS } from '../../lib/releases'
import { DEPLOYMENT_STATUS, DEPLOYMENT_STATUSES, listDeployments, wordOf } from '../../lib/productionQa'
import FormTextField from '../../ui/FormTextField'
import {
  getReleaseItems, getStageReleases, pipelineItemsKey, releaseParts, type ItemFilters, type PipelineItem, type ReleaseFold,
  type ReleaseParts, type ReleaseRun,
} from '../../lib/pipelineBoard'
import type { PipelineTask } from '../../types'
import { FONT_SIZE, FONT_WEIGHT, LINE_HEIGHT, SIZE, grid } from '../../theme'
import { PHONE } from '../../ui/listStyles'
import Tip from '../../ui/Tip'

/* PRODUCTION'S RELEASES (AC-10) — a release-target stage's section body: the last release open, its items grouped by
 * feature (the task's feature; items filed in the project itself last, under no heading; automatic work last of all, in
 * one Automatic row, `renderAutomatic`), and each earlier release folded to one line — version, change
 * count (its words on hover) · age, and Run — that unfolds to its own items by feature
 * (`GET …/pipeline/releases/{id}/items`). The rows are the board's own (`renderTasks`, every row verb kept); a change
 * with no task is a plain line. Under the list's filters only the releases that carried a listed item show, each
 * with those items alone. Run opens the deploy run (`/deployments/:id`); the run's status and failing step show
 * beside it only when it failed or is still under way. Under them, folded, every deploy run of the project
 * (`GET /api/deployments`, any environment, a release's or not), by environment and status — Production QA's, so only
 * on a plan with it. */

const HEAD_SX = {
  display: 'flex', alignItems: 'center', flexWrap: 'wrap', columnGap: 1, minHeight: grid(4.5), width: '100%',
  justifyContent: 'flex-start', textAlign: 'left', borderBottom: '1px solid', borderColor: 'divider',
  [PHONE]: { minHeight: SIZE.tapTarget },
} as const
const CARET_SX = { width: grid(2), color: 'text.secondary', flexShrink: 0 } as const
const VERSION_SX = { fontWeight: FONT_WEIGHT.semibold, fontSize: FONT_SIZE.sm } as const
const META_SX = { fontSize: FONT_SIZE.sm, color: 'text.secondary', lineHeight: LINE_HEIGHT.snug } as const
const FEATURE_SX = { fontSize: FONT_SIZE.sm, fontWeight: FONT_WEIGHT.semibold, color: 'text.secondary', pl: 4, pt: 1 } as const
export const CHANGE_ROW_SX = {
  display: 'flex', alignItems: 'center', gap: 1, minHeight: grid(4), pl: 4, fontSize: FONT_SIZE.md,
  borderBottom: '1px solid', borderColor: 'divider',
} as const

/** A run still going: the only non-error status a release's head names. */
const RUN_UNDER_WAY = new Set(['queued', 'in_progress'])

/** The run beside a release: its status word, and where it failed. `quiet`: only a failure or a run still under way
 *  says anything — a healthy finished deploy is the release's normal, not news. */
function RunMeta({ run, quiet = false }: { run: ReleaseRun | null; quiet?: boolean }): JSX.Element | null {
  if (!run) return null
  const status = wordOf(DEPLOYMENT_STATUS, run.status)
  if (quiet && status.tone !== 'error' && !RUN_UNDER_WAY.has(run.status)) return null
  const failedAt = run.failing_job ? ` at ${run.failing_job}${run.failing_step ? ` › ${run.failing_step}` : ''}` : ''
  return (
    <Box component="span" data-testid="release-run-status"
      sx={{ ...META_SX, ...(status.tone === 'error' ? { color: 'error.main' } : {}) }}>
      {`${status.label}${failedAt}`}
    </Box>
  )
}

export type ReleaseListProps = {
  projectId: number
  stageId: string
  /** The list's filters (`PipelineSections`). */
  filters: ItemFilters
  /** The board's rows for some tasks (every row verb): the section's feed; `parts`, the release's epics and the
   *  children each ships (`TaskListViewProps.parts`). */
  renderTasks: (key: string, tasks: PipelineTask[], parts: ReleaseParts) => ReactNode
  /** A release's automatic tasks (`PipelineItem.automatic`), out of their feature groups: one Automatic row last. */
  renderAutomatic: (key: string, tasks: PipelineTask[]) => ReactNode
  /** Shapes an item's task as the section's rows are shaped. */
  shape: (item: PipelineItem) => PipelineTask | null
  /** Drawn when the stage has had no release: the section's plain rows. */
  fallback: ReactNode
  /** The project's deploy runs under the releases: the last stage's alone, never once per stage. */
  withRuns?: boolean
}

export default function ReleaseList({ projectId, stageId, filters, renderTasks, renderAutomatic, shape, fallback, withRuns = true }: ReleaseListProps): JSX.Element {
  const { admin } = useAuth()
  const runPage = planSurfaceShown(admin, 'production_qa')
  const read = useQuery({
    queryKey: [...pipelineItemsKey(projectId), 'releases', stageId, JSON.stringify(filters)],
    queryFn: ({ signal }) => getStageReleases(projectId, stageId, filters, signal),
    meta: { persist: false },
  })
  if (read.isError) {
    return <Box role="alert" sx={{ ...META_SX, color: 'error.main', py: 1 }}>{apiDetail(read.error, 'Could not load releases')}</Box>
  }
  const releases = read.data?.releases ?? []
  const runs = runPage && withRuns ? <RunsFold projectId={projectId} /> : null
  if (!read.data || releases.length === 0) return <>{fallback}{read.data && runs}</>
  const rows = read.data.rows
  return (
    <Box data-testid="release-list">
      {releases.map(release => (
        <ReleaseFoldRow key={`${release.id}:${release.open}`} projectId={projectId} release={release} rows={release.open ? rows : null}
          filters={filters} renderTasks={renderTasks} renderAutomatic={renderAutomatic} shape={shape} runPage={runPage} />
      ))}
      {runs}
    </Box>
  )
}

/** One release: its head (version, change count · age, Run) and, unfolded, its items by feature. The release view's
 *  deploy in progress (`ReleasePick`) draws one too, open, before its version is pinned. */
export function ReleaseFoldRow({ projectId, release, rows, filters, renderTasks, renderAutomatic, shape, runPage, live = false }: {
  projectId: number
  filters: ItemFilters
  /** The run page is Production QA's: without it, no Run (the status stays). */
  runPage: boolean
  /** A deploy under way: its items re-read, so the version shows once pinned. */
  live?: boolean
  release: ReleaseFold
  /** The open release's rows, read with the stage; a folded one reads its own on unfold. */
  rows: PipelineItem[] | null
  renderTasks: ReleaseListProps['renderTasks']
  renderAutomatic: ReleaseListProps['renderAutomatic']
  shape: ReleaseListProps['shape']
}): JSX.Element {
  const [open, setOpen] = useState(release.open)
  const items = useQuery({
    queryKey: [...pipelineItemsKey(projectId), 'release', release.id, JSON.stringify(filters)],
    queryFn: () => getReleaseItems(projectId, release.id, filters),
    enabled: open && rows === null,
    refetchInterval: live ? RELEASE_POLL_MS : false,
    meta: { persist: false },
  })
  const groups = rows !== null ? release.features : items.data?.features ?? []
  // Items outside any feature (a null `name`) close the list, with no heading of their own.
  const features = [...groups.filter(g => g.name !== null), ...groups.filter(g => g.name === null)]
  const byKey = new Map((rows ?? items.data?.rows ?? []).map(item => [item.key, item]))
  // Automatic work leaves its feature group for the release's one Automatic row, in the carried order.
  const automatic = features.flatMap(g => g.items.flatMap(key => {
    const item = byKey.get(key)
    const task = item?.automatic ? shape(item) : null
    return task ? [task] : []
  }))
  const run = release.run ?? items.data?.run ?? null
  // An epic in the list stands for the parts this release ships, never the rest still being built.
  const parts = releaseParts(rows !== null ? release.parts : items.data?.parts)
  const headId = `release-${release.id}`
  return (
    <Box component="section" aria-labelledby={headId} data-testid="release-fold" data-open={open}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
        <ButtonBase id={headId} data-testid="release-fold-head" aria-expanded={open} onClick={() => setOpen(o => !o)} sx={HEAD_SX}>
          <Box component="span" aria-hidden sx={CARET_SX}>{open ? '▾' : '▸'}</Box>
          <Box component="span" sx={VERSION_SX}>{release.version || items.data?.version || 'New version'}</Box>
          <Box component="span" sx={META_SX}>
            <Tip title={countOf(release.count, 'change')}>
              <Box component="span" data-testid="release-fold-count" aria-label={countOf(release.count, 'change')}>{release.count}</Box>
            </Tip>
            {release.at && ` · ${compactAge(release.at)}`}
          </Box>
          <RunMeta run={run} quiet />
        </ButtonBase>
        {run && runPage && (
          <Button size="small" component={RouterLink} to={`/deployments/${run.id}`} data-testid="release-fold-run"
            sx={{ flexShrink: 0, minWidth: 0, [PHONE]: { minHeight: SIZE.tapTarget } }}>
            Run
          </Button>
        )}
      </Box>
      {open && rows === null && items.isPending && <Box sx={{ ...META_SX, pl: 4, py: 1 }}>Loading…</Box>}
      {open && items.isError && (
        <Box role="alert" sx={{ ...META_SX, color: 'error.main', pl: 4, py: 1 }}>{apiDetail(items.error, 'Could not load the release')}</Box>
      )}
      {open && features.map(feature => {
        const members = feature.items.flatMap(key => (byKey.has(key) && !byKey.get(key)!.automatic ? [byKey.get(key)!] : []))
        const tasks = members.flatMap(item => { const t = shape(item); return t ? [t] : [] })
        const changes = members.filter(item => !item.task && item.change)
        if (tasks.length === 0 && changes.length === 0) return null
        const key = feature.name ?? ''
        return (
          <Box key={key} data-testid="release-feature" data-feature={key}>
            {feature.name !== null && (
              <Box component="h3" sx={{ ...FEATURE_SX, m: 0 }}>{`${feature.name} · ${tasks.length + changes.length}`}</Box>
            )}
            {tasks.length > 0 && renderTasks(`${release.id}:${key}`, tasks, parts)}
            {changes.map(({ key, change }) => change && (
              <Box key={key} data-testid="pipeline-change" sx={CHANGE_ROW_SX}>
                <Box component="span" sx={{ flex: '1 1 auto', minWidth: 0, overflowWrap: 'anywhere' }}>{change.title}</Box>
                {change.merged_at && <Box component="span" sx={META_SX}>{compactAge(change.merged_at)}</Box>}
              </Box>
            ))}
          </Box>
        )
      })}
      {open && automatic.length > 0 && renderAutomatic(`${release.id}:automatic`, automatic)}
    </Box>
  )
}

const STATUS_OPTIONS = [{ value: '', label: 'Any status' }, ...DEPLOYMENT_STATUSES.map(v => ({ value: v, label: DEPLOYMENT_STATUS[v].label }))]

/** Every deploy run of the project, newest first, folded: each opens its run page. */
function RunsFold({ projectId }: { projectId: number }): JSX.Element {
  const [open, setOpen] = useState(false)
  const [environment, setEnvironment] = useState('')
  const [status, setStatus] = useState('')
  const runs = useQuery({
    queryKey: ['deployments', projectId, environment, status],
    queryFn: () => listDeployments({ project_id: String(projectId), environment, status }),
    enabled: open,
    refetchInterval: open ? 15_000 : false,
    meta: { persist: false },
  })
  const seen = useState(() => new Set<string>())[0]
  for (const run of runs.data?.items ?? []) seen.add(run.environment)
  const envOptions = [{ value: '', label: 'Any environment' }, ...[...seen].sort().map(v => ({ value: v, label: v }))]
  return (
    <Box component="section" aria-label="Runs" data-testid="runs-fold" data-open={open}>
      <ButtonBase data-testid="runs-fold-head" aria-expanded={open} onClick={() => setOpen(o => !o)} sx={HEAD_SX}>
        <Box component="span" aria-hidden sx={CARET_SX}>{open ? '▾' : '▸'}</Box>
        <Box component="span" sx={{ fontSize: FONT_SIZE.sm, fontWeight: FONT_WEIGHT.semibold }}>Runs</Box>
      </ButtonBase>
      {open && (
        <Box sx={{ pl: 4 }}>
          <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, py: 1 }}>
            <Box sx={{ minWidth: SIZE.fieldMd }}>
              <FormTextField id="runs-environment" label="Environment" value={environment} onChange={setEnvironment}
                options={envOptions} testId="runs-environment" />
            </Box>
            <Box sx={{ minWidth: SIZE.fieldMd }}>
              <FormTextField id="runs-status" label="Status" value={status} onChange={setStatus}
                options={STATUS_OPTIONS} testId="runs-status" />
            </Box>
          </Box>
          {runs.isError && <Box role="alert" sx={{ ...META_SX, color: 'error.main' }}>{apiDetail(runs.error, 'Could not load runs')}</Box>}
          {runs.data?.items.length === 0 && <Box sx={{ ...META_SX, py: 1 }}>No runs</Box>}
          {runs.data?.items.map(run => (
            <ButtonBase key={run.id} component={RouterLink} to={`/deployments/${run.id}`} data-testid="run-row"
              sx={{ ...CHANGE_ROW_SX, pl: 0, width: '100%', justifyContent: 'flex-start', textAlign: 'left', flexWrap: 'wrap',
                [PHONE]: { minHeight: SIZE.tapTarget } }}>
              <Box component="span" sx={{ fontWeight: FONT_WEIGHT.medium }}>{run.environment}</Box>
              <RunMeta run={{ id: run.id, status: run.status, failing_job: run.failing_job, failing_step: run.failing_step }} />
              {(run.finished_at ?? run.started_at) && (
                <Box component="span" sx={{ ...META_SX, ml: 'auto' }}>{compactAge((run.finished_at ?? run.started_at)!)}</Box>
              )}
            </ButtonBase>
          ))}
        </Box>
      )}
    </Box>
  )
}
