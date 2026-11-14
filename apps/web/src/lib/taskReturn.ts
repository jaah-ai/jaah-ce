import type { PipelineTask } from '../types'
import { queryClient } from '../queryClient'
import { lastBoardScope } from './boardScope'
import {
  getPipelineItemsPage, getPipelineView, PIPELINE_ALL_ITEMS_KEY, pipelineItemsKey, type PipelineItem, type PipelineView,
  type ProjectStageItems, type ViewStage,
} from './pipelineBoard'
import { taskDeepLinkPath } from './taskDeepLink'
import { parseScope, scopeKey, type TaskScope } from './taskScope'

/* WHERE SAVE RETURNS: the board this tab last showed (`lastBoardScope`), the saved row revealed (`taskDeepLinkPath`),
 * opened on a STAGE rather than the whole list — the stage the task sits in (its place on the pipeline,
 * `apps/api/app/pipeline/membership.py::locate`), else the project's first stage (Ideas).
 *
 * The editor navigates AT ONCE and straight ONTO that stage (`?stage=` in the link), read from the board's cached
 * reads: the newest stage list holding the row (a new task: the project's strip, for its first stage). The board keeps
 * that stage while it restores its remembered filters (`pages/Tasks.tsx`). Only when this tab holds neither does the
 * board pick the stage once landed (`savedReturnStage`), showing its loading list meanwhile, never the whole list. A
 * read that fails, a project with no stages, or a board on another project leaves the stage as the board opened. */

/** The navigation-state key carrying a `SavedReturn` to `/tasks`. */
export const SAVED_RETURN_STATE = 'savedReturn'

/** `stage`: the `?stage=` the link already carries, or null when the board must look it up. */
export type SavedReturn = { taskId: number; projectId: number; stage: string | null }

type Saved = Pick<PipelineTask, 'id' | 'project_id' | 'feature_id'>

/** `created`: a task no stage has listed yet, which lands on the project's first stage (Ideas). */
export function savedTaskReturn(
  task: Saved, { created = false }: { created?: boolean } = {},
): { path: string; state: Record<string, SavedReturn> } {
  const kept = lastBoardScope()
  const stage = knownStage(task, kept === null ? null : parseScope(kept), created)
  // A fresh tab lands on the row's project: named as the scope when a stage rides along, so the first paint is
  // that project's stage rather than All projects' list.
  const scope = kept ?? (stage === null ? null : scopeKey({ kind: 'project', projectId: task.project_id }))
  return {
    path: taskDeepLinkPath(task.id, task.project_id, scope, stage),
    state: { [SAVED_RETURN_STATE]: { taskId: task.id, projectId: task.project_id, stage } },
  }
}

export function isSavedReturn(value: unknown): value is SavedReturn {
  if (typeof value !== 'object' || value === null) return false
  const v = value as Record<string, unknown>
  return typeof v.taskId === 'number' && typeof v.projectId === 'number' &&
    (typeof v.stage === 'string' || v.stage === null)
}

/** The landing stage from this tab's cache alone, spelled as the landing board picks it; null when the cache does
 *  not place the task (the board then looks it up) or the board shows another project, whose strip has no stage of
 *  this task's. */
function knownStage(task: Saved, scope: TaskScope | null, created: boolean): string | null {
  const acrossProjects = scope?.kind === 'all'
  const sameProject = scope === null || (scope.kind === 'project' && scope.projectId === task.project_id) ||
    (scope.kind === 'feature' && task.feature_id === scope.featureId)
  if (!sameProject && !acrossProjects) return null
  const stages = cachedStages(task.project_id)
  if (created) {
    const first = stages?.[0]
    return first ? (acrossProjects ? first.name : first.id) : null
  }
  const placed = cachedPlacement(task)
  if (placed === null) return null
  if (acrossProjects) return placed.name ?? stages?.find(s => s.id === placed.id)?.name ?? null
  return placed.id ?? stages?.find(s => s.name === placed.name)?.id ?? null
}

type Placement = { id: string | null; name: string | null }

/** Where the NEWEST list this tab holds places the task: a project's stage list (by id) or All projects' (by name,
 *  the key's). An older list may still hold the row on a stage it has since left. */
function cachedPlacement(task: Saved): Placement | null {
  const cache = queryClient.getQueryCache()
  let best: { at: number; placed: Placement } | null = null
  const consider = (at: number, placed: Placement) => {
    if (best === null || at > best.at) best = { at, placed }
  }
  for (const query of cache.findAll({ queryKey: pipelineItemsKey(task.project_id) })) {
    const row = cachedRows(query.state.data).find(r => r.task?.id === task.id && r.stage_id)
    if (row) consider(query.state.dataUpdatedAt, { id: row.stage_id, name: null })
  }
  for (const query of cache.findAll({ queryKey: PIPELINE_ALL_ITEMS_KEY })) {
    const projects = (query.state.data as { projects?: ProjectStageItems[] } | undefined)?.projects ?? []
    const held = projects.some(p => p.project_id === task.project_id && p.rows.some(r => r.task?.id === task.id))
    const name = query.queryKey[1]
    if (held && typeof name === 'string') consider(query.state.dataUpdatedAt, { id: null, name })
  }
  return (best as { placed: Placement } | null)?.placed ?? null
}

/** Item lists are paged (`{ pages: [{ rows }] }`) or one read (`{ rows }`). */
function cachedRows(data: unknown): PipelineItem[] {
  const pages = (data as { pages?: unknown } | undefined)?.pages
  const reads = Array.isArray(pages) ? pages : [data]
  return reads.flatMap(read => {
    const rows = (read as { rows?: unknown } | undefined)?.rows
    return Array.isArray(rows) ? rows as PipelineItem[] : []
  })
}

/** The project's stages from any strip this tab read (they are the same under every filter). */
function cachedStages(projectId: number): ViewStage[] | null {
  for (const [, data] of queryClient.getQueriesData<PipelineView>({ queryKey: ['pipeline-view', projectId] })) {
    if (data && Array.isArray(data.stages)) return data.stages
  }
  return null
}

async function landingStage(ret: SavedReturn): Promise<ViewStage | null> {
  const [view, items] = await Promise.all([
    getPipelineView(ret.projectId, {}),
    getPipelineItemsPage(ret.projectId, { stage: null, group: null }, { q: String(ret.taskId), showHidden: true }, null),
  ])
  const own = items.rows.find(row => row.task?.id === ret.taskId)?.stage_id
  return view.stages.find(stage => stage.id === own) ?? view.stages[0] ?? null
}

/** The `?stage=` pick for a return onto `scope` (whose one project is `boardProjectId`), spelled as that board picks
 *  it: a stage id on one project's strip, a stage name on All projects' totals. `null`: leave the stage alone. */
export async function savedReturnStage(
  ret: SavedReturn, scope: TaskScope, boardProjectId: number | null,
): Promise<string | null> {
  const acrossProjects = scope.kind === 'all'
  if (!acrossProjects && boardProjectId !== ret.projectId) return null
  const stage = await landingStage(ret).catch(() => null)
  if (stage === null) return null
  return acrossProjects ? stage.name : stage.id
}
