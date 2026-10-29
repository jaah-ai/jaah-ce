import type { TaskTree, TaskTreeCounts } from '../types'
import type { TaskScope } from './taskScope'
import { isUuidShaped } from './uuidShape'

/* A USER'S MUTED PROJECTS AND FEATURES — stored server-side as `task_muted`
 * (`apps/api/app/preferences.py`), a list of `scopeKey`s (`project:<id>`, `feature:<uuid>`). This user's alone.
 *
 * THE RULE: a muted node's tasks show only when that node, or something under it, is selected. Above it they
 * are gone: the board's list and its counts drop them (`muteExclusionParams`, sent as the server's
 * `exclude_*` params), and no parent's counts include them (`applyMutes`). The rail still lists a muted node,
 * greyed and without its pills or progress ring, so it can be selected and unmuted. Pure of React. */

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
  'stopped', 'questions', 'failed', 'paused', 'waiting', 'in_progress', 'queued', 'draft', 'done', 'total',
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
 *  feature or "Other" drops nothing, being at or under whatever is muted. */
export function muteExclusionParams(scope: TaskScope, muted: readonly string[]): [string, string][] {
  if (scope.kind === 'feature' || scope.kind === 'unfiled') return []
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
