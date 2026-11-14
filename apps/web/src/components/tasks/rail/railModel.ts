import type { TaskTree, TaskTreeCounts, TaskTreeFeature, TaskTreeProject } from '../../../types'
import { locateScope, scopeKey, type TaskScope } from '../../../lib/taskScope'

/* THE RAIL'S SHAPE, as data — what `TaskTree.tsx` renders, kept pure so the rules are testable without a DOM.
 *
 *   Pinned            (only when something is pinned)
 *   All projects
 *     Project
 *       Feature       (in the project's shared order: `PUT /api/features/order`)
 *
 * A project-level task (one outside every feature) is the project's alone, and the project's row carries it.
 *
 * The rail shows containers, never individual tasks (D-rail). Every node carries its counts; a parent's are the
 * server's roll-up of its children's. THE STOPPED PILL shows each stopped task exactly once on screen
 * (`pillCounts`): a closed branch carries its whole tree's, an open one only what no child row shows, and All
 * projects none. */

export type RailNodeKind = 'all' | 'project' | 'feature'

export type RailNode = {
  /** The row's identity: its scope's `scopeKey`. Also its `task_rail_expanded` entry. */
  key: string
  kind: RailNodeKind
  /** What selecting the row shows. */
  scope: TaskScope
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

/** Is this row hidden (`lib/taskMute.ts`): its own node, or any row under a hidden project? Drawn only while the
 *  rail shows hidden rows (`visibleRows`' `showHidden`), greyed, with no pill and no ring. */
export function isMutedNode(node: RailNode): boolean {
  if (node.kind === 'all') return false
  if (node.project?.muted === true) return true
  return node.kind === 'feature' && node.feature?.muted === true
}

export const ALL_KEY = 'all'

/** Is `key` a node the tree still holds? Stale pins and expansions are dropped on read. */
export function keyInTree(key: string, tree: TaskTree): boolean {
  if (key === ALL_KEY) return true
  for (const project of tree.projects) {
    if (key === `project:${project.id}`) return true
    if (project.features.some(f => key === `feature:${f.id}`)) return true
  }
  return false
}

/** The keys that must be open for `scope`'s own row to show: All projects and, for a feature, its project. */
export function ancestorKeys(scope: TaskScope, tree: TaskTree | undefined): string[] {
  const { project } = locateScope(scope, tree)
  if (!project) return []
  if (scope.kind === 'project') return [ALL_KEY]
  return [ALL_KEY, `project:${project.id}`]
}

function sumCounts(parts: TaskTreeCounts[]): TaskTreeCounts {
  const total: TaskTreeCounts = { stopped: 0, questions: 0, failed: 0, paused: 0, in_progress: 0, queued: 0, draft: 0, suggested: 0, done: 0, total: 0 }
  for (const c of parts) {
    for (const k of Object.keys(total) as (keyof TaskTreeCounts)[]) total[k] = (total[k] ?? 0) + (c[k] ?? 0)
  }
  return total
}

/** A project's children, at level 3: its features. */
function projectChildren(project: TaskTreeProject, parentKey: string, out: RailNode[], showHidden: HiddenRule) {
  for (const feature of shownFeatures(project, showHidden)) {
    const scope: TaskScope = { kind: 'feature', featureId: feature.id }
    out.push({
      key: scopeKey(scope), kind: 'feature', scope, label: feature.name, level: 3, counts: feature.counts,
      expandable: false, expanded: false, parentKey, project, feature,
    })
  }
}

/** Which hidden rows the rail draws: all of them (the find box's eye), or only `keep`'s — the board's open node
 *  and its ancestors, so hiding what is open never leaves the board with no lit row. */
export type HiddenRule = boolean | ReadonlySet<string>

function drawsHidden(rule: HiddenRule, key: string): boolean {
  return rule === true || (rule !== false && rule.has(key))
}

/** The project's features the rail draws: the ones not hidden, and hidden ones `rule` keeps. */
function shownFeatures(project: TaskTreeProject, rule: HiddenRule): TaskTreeFeature[] {
  return project.features.filter(f => !f.muted || drawsHidden(rule, `feature:${f.id}`))
}

/** Does the project row fold? Only when it has a feature row to show — never a lone empty wrapper. */
function hasChildren(project: TaskTreeProject, showHidden: HiddenRule): boolean {
  return shownFeatures(project, showHidden).length > 0
}

/** The tree's VISIBLE rows, in order, below All projects (which is always open). A hidden project or feature
 *  (`lib/taskMute.ts`) has no row unless `showHidden` draws it (`HiddenRule`). */
export function visibleRows(tree: TaskTree, isOpen: (key: string) => boolean, showHidden: HiddenRule = false): RailNode[] {
  const out: RailNode[] = [{
    key: ALL_KEY, kind: 'all', scope: { kind: 'all' }, label: 'All projects', level: 1, counts: tree.counts,
    expandable: false, expanded: true, parentKey: null,
  }]
  for (const project of tree.projects) {
    const scope: TaskScope = { kind: 'project', projectId: project.id }
    const key = scopeKey(scope)
    if (project.muted && !drawsHidden(showHidden, key)) continue
    const expandable = hasChildren(project, showHidden)
    const open = expandable && isOpen(key)
    out.push({
      key, kind: 'project', scope, label: project.name, level: 2, counts: project.counts,
      expandable, expanded: open, parentKey: ALL_KEY, project,
    })
    if (open) projectChildren(project, key, out, showHidden)
  }
  return out
}

/** One node the tree holds, resolved by key (for the Pinned group); a hidden one only when `showHidden`. */
export function nodeForKey(key: string, tree: TaskTree, showHidden = false): RailNode | undefined {
  // Every node is reachable with everything open.
  return visibleRows(tree, () => true, showHidden).find(n => n.key === key && n.kind !== 'all')
}

/** The counts `node`'s Stopped pill shows, or `undefined` for none. All projects: never. A project: its whole
 *  tree while closed (`asLeaf`: a pinned or matched row, which never opens, counts as closed); open, only what no
 *  feature row shows — its counts without its shown features' (a hidden feature's are already out of them,
 *  `lib/taskMute.ts::applyMutes`). A feature: its own. */
export function pillCounts(node: RailNode, asLeaf = false): TaskTreeCounts | undefined {
  switch (node.kind) {
    case 'all':
      return undefined
    case 'project': {
      if (asLeaf || !node.expanded) return node.counts
      const shown = sumCounts(node.project!.features.filter(f => !f.muted).map(f => f.counts))
      const own = { ...node.counts }
      for (const k of Object.keys(own) as (keyof TaskTreeCounts)[]) own[k] = Math.max(0, (own[k] ?? 0) - (shown[k] ?? 0))
      return own
    }
    default:
      return node.counts
  }
}

/* THE STOPPED MARK: one pill per kind, stacked. The FRONT pill is the most urgent kind present (a question
   awaiting the operator first, then failed, paused) and carries that kind's own count; any other kind
   present peeks out behind it as a bare pill in its colour. The noun for every kind is on the stack's title and
   the row's accessible name. The glyph is the task pane's (`pane/stopReason.ts`), never the pill's. */
export const STOP_KINDS = [
  { field: 'failed', glyph: '!', noun: ['failed', 'failed'], bg: 'error.main', fg: 'error.contrastText' },
  { field: 'questions', glyph: '?', noun: ['question', 'questions'], bg: 'warning.main', fg: 'warning.contrastText' },
  { field: 'paused', glyph: '⏸', noun: ['paused', 'paused'], bg: 'grey.600', fg: 'common.white' },
] as const

export type StopKind = (typeof STOP_KINDS)[number]

const STACK_ORDER: readonly StopKind['field'][] = ['questions', 'failed', 'paused']

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
   projects and features, hidden ones only when `showHidden` — as a flat list with the
   path it sits under. */
export type RailMatch = { node: RailNode; path: string; start: number }

export function filterTree(tree: TaskTree, term: string, showHidden = false): RailMatch[] {
  const needle = term.trim().toLowerCase()
  if (needle === '') return []
  const out: RailMatch[] = []
  for (const node of visibleRows(tree, () => true, showHidden)) {
    if (node.kind === 'all') continue
    const start = node.label.toLowerCase().indexOf(needle)
    if (start < 0) continue
    const path: string[] = []
    if (node.kind !== 'project' && node.project) path.push(node.project.name)
    out.push({ node, path: path.join(' › '), start })
  }
  return out
}

/* THE FEATURE ORDER: a drag (or Move up / Move down) moves one feature before or after another of the same
   project. `PUT /api/features/order` takes the project's whole list, so these compute it. */

/** The project's feature ids with `featureId` moved to `edge` of `targetId`; `undefined` when nothing would
 *  change (or either is not a feature of the project). */
export function reorderedIds(
  tree: TaskTree, projectId: number, featureId: string, targetId: string, edge: 'before' | 'after',
): string[] | undefined {
  const project = tree.projects.find(p => p.id === projectId)
  if (!project || featureId === targetId) return undefined
  const current = project.features.map(f => f.id)
  if (!current.includes(featureId) || !current.includes(targetId)) return undefined
  const next = current.filter(id => id !== featureId)
  next.splice(next.indexOf(targetId) + (edge === 'after' ? 1 : 0), 0, featureId)
  return next.every((id, i) => id === current[i]) ? undefined : next
}

/** `tree` with the project's features in `ids`' order, any `ids` misses after them as they were. */
export function withFeatureOrder(tree: TaskTree, projectId: number, ids: string[]): TaskTree {
  return {
    ...tree,
    projects: tree.projects.map(p => {
      if (p.id !== projectId) return p
      const byId = new Map(p.features.map(f => [f.id, f]))
      const ordered = ids.flatMap(id => byId.get(id) ?? [])
      return { ...p, features: [...ordered, ...p.features.filter(f => !ids.includes(f.id))] }
    }),
  }
}
