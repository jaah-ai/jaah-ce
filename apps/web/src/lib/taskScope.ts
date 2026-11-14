import type { TaskTree, TaskTreeFeature, TaskTreeProject } from '../types'
import { muteExclusionParams } from './taskMute'
import { isUuidShaped } from './uuidShape'

/* THE TASK SCOPE — which set of tasks the board shows, and which rail node is selected. The PINNED contract the
 * rail (`components/tasks/rail/`), the pane header and body import; `?scope=` on `/tasks` spells it with
 * `scopeKey`, and the same keys are the `task_rail_pins` / `task_rail_expanded` preference entries
 * (`apps/api/app/preferences.py`).
 *
 * Levels (the settled glossary): Project (one repo) › Feature › Task. A project-level task (one outside every
 * feature) belongs to its project alone, so the project's scope holds it. */

export type TaskScope =
  | { kind: 'all' }
  | { kind: 'project'; projectId: number }
  | { kind: 'feature'; featureId: string }

/** The `/tasks` query param carrying `scopeKey(scope)`. */
export const SCOPE_PARAM = 'scope'

export const ALL_SCOPE: TaskScope = { kind: 'all' }

/* `unfiled:<id>` is a retired spelling of the project's own scope; a stored or linked one still opens it. */
const PROJECT_KEY = /^(?:project|unfiled):([1-9][0-9]*)$/

/** A raw `?scope=` (or preference key) as a scope. TOTAL: absent, blank, junk or another retired key is All
 *  projects. */
export function parseScope(raw: string | null): TaskScope {
  if (raw === null) return ALL_SCOPE
  const project = PROJECT_KEY.exec(raw)
  if (project) {
    const projectId = Number(project[1])
    if (!Number.isSafeInteger(projectId)) return ALL_SCOPE
    return { kind: 'project', projectId }
  }
  const prefix = 'feature:'
  if (raw.startsWith(prefix) && isUuidShaped(raw.slice(prefix.length))) {
    return { kind: 'feature', featureId: raw.slice(prefix.length) }
  }
  return ALL_SCOPE
}

/** The scope's one spelling: the URL value, the rail-highlight key, and the pin/expansion preference entry. */
export function scopeKey(s: TaskScope): string {
  switch (s.kind) {
    case 'all':
      return 'all'
    case 'project':
      return `project:${s.projectId}`
    case 'feature':
      return `feature:${s.featureId}`
  }
}

/** `/tasks` scoped to `s`. */
export function scopeHref(s: TaskScope): string {
  return `/tasks?${SCOPE_PARAM}=${encodeURIComponent(scopeKey(s))}`
}

/** `GET /api/tasks`' narrowing params for `s`. `muted` (the
 *  user's `task_muted`) adds the `exclude_*` params for the muted nodes above `s` (`lib/taskMute.ts`). */
export function scopeToListParams(s: TaskScope, muted: readonly string[] = []): URLSearchParams {
  const params = new URLSearchParams(muteExclusionParams(s, muted))
  switch (s.kind) {
    case 'all':
      break
    case 'project':
      params.set('project_id', String(s.projectId))
      break
    case 'feature':
      params.set('feature_id', s.featureId)
      break
  }
  return params
}

/** Where a node sits in the tree. */
export type TreeLocation = {
  project?: TaskTreeProject
  feature?: TaskTreeFeature
}

/** The tree nodes `s` names and sits under; empty where the tree does not (yet) hold them. */
export function locateScope(s: TaskScope, tree: TaskTree | undefined): TreeLocation {
  if (!tree || s.kind === 'all') return {}
  for (const project of tree.projects) {
    if (s.kind === 'feature') {
      const feature = project.features.find(f => f.id === s.featureId)
      if (feature) return { project, feature }
    } else if (project.id === s.projectId) {
      return { project }
    }
  }
  return {}
}

/** The projects a selection lets the user create a feature in (the tree's `can_create_tasks`; the API refuses the
 *  rest anyway) — every creatable project under All projects, else the selection's own project. */
export function creatableFor(s: TaskScope, tree: TaskTree | undefined): TaskTreeProject[] {
  const all = (tree?.projects ?? []).filter(p => p.can_create_tasks)
  if (s.kind === 'all') return all
  const { project } = locateScope(s, tree)
  return project ? all.filter(p => p.id === project.id) : []
}

/** The single project `s` lives in — where a new task is filed — or `null` for All projects or a node the tree
 *  does not hold yet. */
export function scopeProjectId(s: TaskScope, tree: TaskTree | undefined): number | null {
  if (s.kind === 'project') return s.projectId
  return locateScope(s, tree).project?.id ?? null
}

/** Is `task` a row of scope `s`? Membership by the row's own `project_id` and `feature_id` — no tree needed, so it
 *  holds for a row the bounded list did not load (the deep link's pin). */
export function taskInScope(s: TaskScope, task: { project_id: number; feature_id?: string | null }): boolean {
  switch (s.kind) {
    case 'all':
      return true
    case 'project':
      return task.project_id === s.projectId
    case 'feature':
      return task.feature_id === s.featureId
  }
}

/** What the board is showing, as one line: the `/tasks` header's title and the phone's scope dropdown. A node the
 *  tree does not hold (loading, stale, not visible) is named by its NOUN — never a blank. */
export function scopeLabel(s: TaskScope, tree: TaskTree | undefined): string {
  if (s.kind === 'all') return 'All projects'
  const { project, feature } = locateScope(s, tree)
  if (s.kind !== 'feature') return project?.name ?? 'Project'
  return feature?.name ?? 'Feature'
}

/**
 * THE BOARD A TASK PAGE (`/tasks/:id`) SITS BESIDE, as the `?scope=` to write onto its URL — or `null` to write
 * nothing. The link's own `urlScope` wins; else the board it was opened from (`boardScope`, that list's
 * `?scope=`); else the task's own project, so a reload or a bare link opens that project's pipeline.
 */
export function taskPageScope(
  urlScope: string | null,
  boardScope: string | null,
  projectId: number | null | undefined,
): string | null {
  if (urlScope) return null
  if (boardScope) return boardScope
  return typeof projectId === 'number' && Number.isSafeInteger(projectId) && projectId > 0
    ? scopeKey({ kind: 'project', projectId })
    : null
}
