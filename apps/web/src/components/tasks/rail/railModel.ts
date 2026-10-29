import type { TaskTree, TaskTreeCounts, TaskTreeFeature, TaskTreeProject } from '../../../types'
import { locateScope, parseScope, scopeKey, type TaskScope } from '../../../lib/taskScope'

/* THE RAIL'S SHAPE, as data — what `TaskTree.tsx` renders, kept pure so the rules are testable without a DOM.
 *
 *   Pinned            (only when something is pinned)
 *   All projects
 *     Project
 *       Feature       (active only, in the project's shared order: `PUT /api/features/order`)
 *       Shipped (n)   (a collapsed group of shipped features)
 *
 * A project's no-feature tasks draw no node of their own: they are the project's, and its row carries them.
 *
 * The rail shows containers, never individual tasks (D-rail). Every node carries its counts; a parent's are the
 * server's roll-up of its children's. THE STOPPED PILL shows each stopped task exactly once on screen
 * (`pillCounts`): a closed branch carries its whole tree's, an open one only what no child row shows, and All
 * projects none. */

export type RailNodeKind = 'all' | 'project' | 'feature' | 'shipped'

export type RailNode = {
  /** The row's identity: a `scopeKey`, or `shipped:<project id>` for the Shipped group. Also its
   *  `task_rail_expanded` entry. */
  key: string
  kind: RailNodeKind
  /** What selecting the row shows; absent for the Shipped group, which only folds. */
  scope?: TaskScope
  label: string
  /** 1-based, as `aria-level`. */
  level: number
  counts: TaskTreeCounts
  /** Does the row fold? */
  expandable: boolean
  expanded: boolean
  parentKey: string | null
  project?: TaskTreeProject
  feature?: TaskTreeFeature
}

/** Is this row muted (`lib/taskMute.ts`): its own node, or any row under a muted project? It draws greyed, with
 *  no pill and no ring. */
export function isMutedNode(node: RailNode): boolean {
  if (node.kind === 'all') return false
  if (node.project?.muted === true) return true
  return node.kind === 'feature' && node.feature?.muted === true
}

/** The key of a project's Shipped group. */
export function shippedKey(projectId: number): string {
  return `shipped:${projectId}`
}

export const ALL_KEY = 'all'

/** Is `key` a node the tree still holds? Stale pins and expansions are dropped on read. */
export function keyInTree(key: string, tree: TaskTree): boolean {
  if (key === ALL_KEY) return true
  for (const project of tree.projects) {
    if (key === `project:${project.id}` || key === shippedKey(project.id)) return true
    if (project.features.some(f => key === `feature:${f.id}`)) return true
  }
  return false
}

/** The keys that must be open for `scope`'s own row to show: its project and, for a shipped feature, its
 *  Shipped group. */
export function ancestorKeys(scope: TaskScope, tree: TaskTree | undefined): string[] {
  const { project, feature } = locateScope(scope, tree)
  if (!project) return []
  const keys = [ALL_KEY]
  // A no-feature set is the project's own, and the project's row is the one lit (`railKey`).
  if (scope.kind === 'project' || scope.kind === 'unfiled') return keys
  keys.push(`project:${project.id}`)
  if (feature && feature.status === 'shipped') keys.push(shippedKey(project.id))
  return keys
}

/** The row that lights for the board's scope key: its own, except a project's no-feature scope, which has no row —
 *  the project's lights. */
export function railKey(key: string): string {
  const scope = parseScope(key)
  return scope.kind === 'unfiled' ? scopeKey({ kind: 'project', projectId: scope.projectId }) : key
}

function sumCounts(parts: TaskTreeCounts[]): TaskTreeCounts {
  const total: TaskTreeCounts = { stopped: 0, questions: 0, failed: 0, paused: 0, waiting: 0, in_progress: 0, queued: 0, draft: 0, done: 0, total: 0 }
  for (const c of parts) {
    for (const k of Object.keys(total) as (keyof TaskTreeCounts)[]) total[k] = (total[k] ?? 0) + (c[k] ?? 0)
  }
  return total
}

/** A project's children, at level 3: active features, then Shipped. */
function projectChildren(
  project: TaskTreeProject,
  parentKey: string,
  isOpen: (key: string) => boolean,
  out: RailNode[],
) {
  const level = 3
  for (const feature of project.features) {
    if (feature.status !== 'active') continue
    const scope: TaskScope = { kind: 'feature', featureId: feature.id }
    out.push({
      key: scopeKey(scope), kind: 'feature', scope, label: feature.name, level, counts: feature.counts,
      expandable: false, expanded: false, parentKey, project, feature,
    })
  }
  const shipped = project.features.filter(f => f.status === 'shipped')
  if (shipped.length === 0) return
  const key = shippedKey(project.id)
  const open = isOpen(key)
  out.push({
    // Without the muted ones' counts, as a project's (`lib/taskMute.ts::applyMutes`).
    key, kind: 'shipped', label: `Shipped (${shipped.length})`, level, counts: sumCounts(shipped.filter(f => !f.muted).map(f => f.counts)),
    expandable: true, expanded: open, parentKey, project,
  })
  if (!open) return
  for (const feature of shipped) {
    const scope: TaskScope = { kind: 'feature', featureId: feature.id }
    out.push({
      key: scopeKey(scope), kind: 'feature', scope, label: feature.name, level: level + 1, counts: feature.counts,
      expandable: false, expanded: false, parentKey: key, project, feature,
    })
  }
}

/** Does the project row fold? Only when it has a feature row to show — never a lone empty wrapper. */
function hasChildren(project: TaskTreeProject): boolean {
  return project.features.length > 0
}

/** The tree's VISIBLE rows, in order, below All projects (which is always open). */
export function visibleRows(tree: TaskTree, isOpen: (key: string) => boolean): RailNode[] {
  const out: RailNode[] = [{
    key: ALL_KEY, kind: 'all', scope: { kind: 'all' }, label: 'All projects', level: 1, counts: tree.counts,
    expandable: false, expanded: true, parentKey: null,
  }]
  for (const project of tree.projects) {
    const scope: TaskScope = { kind: 'project', projectId: project.id }
    const key = scopeKey(scope)
    const expandable = hasChildren(project)
    const open = expandable && isOpen(key)
    out.push({
      key, kind: 'project', scope, label: project.name, level: 2, counts: project.counts,
      expandable, expanded: open, parentKey: ALL_KEY, project,
    })
    if (open) projectChildren(project, key, isOpen, out)
  }
  return out
}

/** One node the tree holds, resolved by key (for the Pinned group). */
export function nodeForKey(key: string, tree: TaskTree): RailNode | undefined {
  // Every node is reachable with everything open.
  return visibleRows(tree, () => true).find(n => n.key === key && n.kind !== 'shipped' && n.kind !== 'all')
}

/** The counts `node`'s Stopped pill shows, or `undefined` for none. All projects: never. A project or the Shipped
 *  group: its whole tree while closed (`asLeaf`: a pinned or matched row, which never opens, counts as closed);
 *  open, only what no child row shows — for a project, its no-feature tasks. A feature: its own. */
export function pillCounts(node: RailNode, asLeaf = false): TaskTreeCounts | undefined {
  switch (node.kind) {
    case 'all':
      return undefined
    case 'project':
      if (asLeaf || !node.expanded) return node.counts
      return node.project!.unfiled ?? undefined
    case 'shipped':
      return asLeaf || !node.expanded ? node.counts : undefined
    default:
      return node.counts
  }
}

/* THE STOPPED MARK: one pill per kind, stacked. The FRONT pill is the most urgent kind present (a question
   awaiting the operator first, then failed, waiting, paused) and carries that kind's own count; any other kind
   present peeks out behind it as a bare pill in its colour. The noun for every kind is on the stack's title and
   the row's accessible name. The glyph is the task pane's (`pane/stopReason.ts`), never the pill's. */
export const STOP_KINDS = [
  { field: 'failed', glyph: '!', noun: ['failed', 'failed'], bg: 'error.main', fg: 'error.contrastText' },
  { field: 'questions', glyph: '?', noun: ['question', 'questions'], bg: 'warning.main', fg: 'warning.contrastText' },
  { field: 'waiting', glyph: '⧗', noun: ['waiting on customer', 'waiting on customer'], bg: 'info.main', fg: 'info.contrastText' },
  { field: 'paused', glyph: '⏸', noun: ['paused', 'paused'], bg: 'grey.600', fg: 'common.white' },
] as const

export type StopKind = (typeof STOP_KINDS)[number]

const STACK_ORDER: readonly StopKind['field'][] = ['questions', 'failed', 'waiting', 'paused']

/** The kinds a node has stopped rows of, front of the stack first; `[]` when none. */
export function stackedStopKinds(counts: TaskTreeCounts): StopKind[] {
  return STACK_ORDER.map(field => STOP_KINDS.find(k => k.field === field)!).filter(k => counts[k.field] > 0)
}

/** "1 question", or "3 stopped: 2 questions, 1 failed" when kinds mix — the pill's title and the accessible half
 *  of the pill; `''` when nothing is stopped. */
export function stoppedPhrase(counts: TaskTreeCounts): string {
  if (counts.stopped <= 0) return ''
  const parts = STOP_KINDS.filter(k => counts[k.field] > 0).map(
    k => `${counts[k.field]} ${k.noun[counts[k.field] === 1 ? 0 : 1]}`,
  )
  // One kind that is the whole count says only itself.
  if (parts.length === 1 && STOP_KINDS.some(k => counts[k.field] === counts.stopped)) return parts[0]
  return `${counts.stopped} stopped${parts.length ? `: ${parts.join(', ')}` : ''}`
}

/* THE FILTER BOX: a client-side, case-insensitive substring match over every container the rail can show —
   projects and features active or shipped — as a flat list with the
   path it sits under. */
export type RailMatch = { node: RailNode; path: string; start: number }

export function filterTree(tree: TaskTree, term: string): RailMatch[] {
  const needle = term.trim().toLowerCase()
  if (needle === '') return []
  const out: RailMatch[] = []
  for (const node of visibleRows(tree, () => true)) {
    if (node.kind === 'all' || node.kind === 'shipped') continue
    const start = node.label.toLowerCase().indexOf(needle)
    if (start < 0) continue
    const path: string[] = []
    if (node.kind !== 'project' && node.project) path.push(node.project.name)
    if (node.kind === 'feature' && node.feature?.status === 'shipped') path.push('Shipped')
    out.push({ node, path: path.join(' › '), start })
  }
  return out
}

/* THE FEATURE ORDER: a drag (or Move up / Move down) moves one active feature before or after another of the
   same project. `PUT /api/features/order` takes the whole active list, so these compute it. */

/** The project's active feature ids with `featureId` moved to `edge` of `targetId`; `undefined` when nothing
 *  would change (or either is not an active feature of the project). */
export function reorderedIds(
  tree: TaskTree, projectId: number, featureId: string, targetId: string, edge: 'before' | 'after',
): string[] | undefined {
  const project = tree.projects.find(p => p.id === projectId)
  if (!project || featureId === targetId) return undefined
  const current = project.features.filter(f => f.status === 'active').map(f => f.id)
  if (!current.includes(featureId) || !current.includes(targetId)) return undefined
  const next = current.filter(id => id !== featureId)
  next.splice(next.indexOf(targetId) + (edge === 'after' ? 1 : 0), 0, featureId)
  return next.every((id, i) => id === current[i]) ? undefined : next
}

/** `tree` with the project's active features in `ids`' order, shipped ones after them as they were. */
export function withFeatureOrder(tree: TaskTree, projectId: number, ids: string[]): TaskTree {
  return {
    ...tree,
    projects: tree.projects.map(p => {
      if (p.id !== projectId) return p
      const byId = new Map(p.features.map(f => [f.id, f]))
      const active = ids.flatMap(id => byId.get(id) ?? [])
      return { ...p, features: [...active, ...p.features.filter(f => !ids.includes(f.id))] }
    }),
  }
}
