import type { TaskTree, TaskTreeCounts } from '../types'
import type { TaskScope } from './taskScope'
import { isUuidShaped } from './uuidShape'

/* A USER'S HIDDEN PROJECTS AND FEATURES — "Hide" / "Show" in the UI, stored server-side as `task_muted` (its
 * name from when the verb was Mute; `apps/api/app/preferences.py`), a list of keys (`project:<id>`,
 * `feature:<uuid>`; any other stored key is ignored). This user's alone. The code calls a hidden node `muted`, after the stored key.
 *
 * THE RULE: a hidden node's tasks show only when that node, or something under it, is selected. Above it they
 * are gone: the board's list and its counts drop them (`muteExclusionParams`, sent as the server's
 * `exclude_*` params), and no parent's counts include them (`applyMutes`). The rail draws no row for a hidden
 * node (`components/tasks/rail/railModel.ts::visibleRows`) until its find box's eye shows hidden rows, greyed and
 * without pills or progress ring, so one can be shown again; the Projects page lists hidden projects greyed, with
 * the same toggle on each row's eye. Pure of React. */

/** The preference key, named once so the writers and the readers cannot drift apart. */
export const MUTED_PREF = 'task_muted'

/** The stored muted keys; anything that is not a list of strings reads as none. */
export function readMuted(preferences: Record<string, unknown> | null | undefined): string[] {
  const raw = preferences?.[MUTED_PREF]
  return Array.isArray(raw) ? raw.filter((v): v is string => typeof v === 'string') : []
}

/** `muted` with `key` added or removed. */
export function toggledMuted(muted: readonly string[], key: string): string[] {
  return muted.includes(key) ? muted.filter(k => k !== key) : [...muted, key]
}

const COUNT_FIELDS: (keyof TaskTreeCounts)[] = [
  'stopped', 'questions', 'failed', 'paused', 'in_progress', 'queued', 'draft', 'suggested', 'done', 'total',
]

function minus(from: TaskTreeCounts, parts: TaskTreeCounts[]): TaskTreeCounts {
  const out = { ...from }
  for (const part of parts) for (const k of COUNT_FIELDS) out[k] = Math.max(0, (out[k] ?? 0) - (part[k] ?? 0))
  return out
}

/** `tree` with each node marked `muted`, and every parent's counts without its muted descendants. A muted
 *  node keeps its OWN counts: they are what it shows when selected. */
export function applyMutes(tree: TaskTree, muted: readonly string[]): TaskTree {
  if (muted.length === 0) return tree
  const set = new Set(muted)
  const dropFromRoot: TaskTreeCounts[] = []
  const projects = tree.projects.map(project => {
    const projectMuted = set.has(`project:${project.id}`)
    const features = project.features.map(f => ({ ...f, muted: set.has(`feature:${f.id}`) }))
    const mutedFeatures = features.filter(f => f.muted).map(f => f.counts)
    // A muted project leaves the root whole (its muted features with it, so they are not taken twice).
    dropFromRoot.push(...(projectMuted ? [project.counts] : mutedFeatures))
    return { ...project, muted: projectMuted, features, counts: minus(project.counts, mutedFeatures) }
  })
  return { ...tree, projects, counts: minus(tree.counts, dropFromRoot) }
}

/** The board's `exclude_*` params for `scope`: the muted nodes ABOVE it. All projects drops every muted
 *  project and feature; a project drops its muted features (another project's are outside it anyway); a
 *  feature drops nothing, being at or under whatever is muted. */
export function muteExclusionParams(scope: TaskScope, muted: readonly string[]): [string, string][] {
  if (scope.kind === 'feature') return []
  const out: [string, string][] = []
  // A key whose id does not parse is skipped: sent, it would 422 every board read.
  for (const key of muted) {
    const project = /^project:([1-9][0-9]*)$/.exec(key)
    if (scope.kind === 'all' && project) out.push(['exclude_project_id', project[1]])
    const featureId = key.startsWith('feature:') ? key.slice('feature:'.length) : ''
    if (isUuidShaped(featureId)) out.push(['exclude_feature_id', featureId])
  }
  return out
}

/** `projects` without this user's hidden ones — what a NEW task may default to or be picked into. A
 *  project in `keep` stays even when hidden: the board the operator opened (or a seed that names it) is a choice,
 *  not a default. Every project hidden leaves none, and the create surfaces offer no default. */
export function withoutHiddenProjects<T extends { id: number }>(
  projects: T[],
  muted: readonly string[],
  keep: readonly (number | null | undefined)[] = [],
): T[] {
  const hidden = new Set(muted)
  return projects.filter(p => keep.includes(p.id) || !hidden.has(`project:${p.id}`))
}
