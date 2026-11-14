import type { PipelineTask, TaskSection, TaskStopKind, TaskTree, TaskTreeCounts } from '../../../types'

/* THE BOARD'S SECTION VOCABULARY, PURE and DOM-free: the State sections' order and labels, the rank of rows inside
 * them, and the Feature axis's buckets. Sections are READ (`PipelineTask.section`), never re-derived. */

/** A DISPLAY section: the server's four plus "Questions", which the board splits out of Stopped, and
 *  "Queued", split out of In progress, and "Suggested by Jaah", split out of Draft (`isSuggestion`). An asking
 *  (`question`) row waits on a PERSON, not on a fix — under "Stopped" it read as broken; a `ready` row runs nothing
 *  yet — under "In progress" it read as working. Display-only: the server still sections them `stopped` /
 *  `in_progress` (one `?section=` page feeds both), and the counts split the same way (`task_tree._KIND_FIELD`:
 *  question → `questions`; `task_tree.QUEUED_SECTION` → `queued`). */
export type BoardSection = 'questions' | 'queued' | 'suggested' | TaskSection

/** Display order of the sections, and of the rows inside two of them (D6-state; the server's
 *  `task_sections.STOP_KIND_ORDER` / `IN_PROGRESS_ORDER`, which already order a `?section=` page). */
export const SECTION_ORDER: readonly BoardSection[] = ['questions', 'stopped', 'in_progress', 'queued', 'draft', 'suggested', 'done']
const STOP_KIND_ORDER: readonly TaskStopKind[] = ['question', 'failed', 'paused', 'balance', 'jaah_unavailable']
// `awaiting_response` is In progress only while its answer is delivering.
const IN_PROGRESS_STATE_ORDER: readonly string[] = ['active', 'awaiting_response', 'ready']

export const SECTION_LABEL: Record<BoardSection, string> = {
  questions: 'Questions',
  stopped: 'Stopped',
  in_progress: 'In progress',
  queued: 'Queued',
  draft: 'Draft',
  suggested: 'Suggested by Jaah',
  done: 'Done',
}

export function sectionOf(task: PipelineTask): TaskSection {
  // Older payloads carry no section: they read as In progress rather than vanishing.
  return task.section ?? 'in_progress'
}

/** A draft a runner filed on its own from another task (`PipelineTask.suggested`): Accept queues it, Discard deletes
 *  it. The single home of the test, for the board's section and both Accept/Discard doors (row and task page). */
export function isSuggestion(task: Pick<PipelineTask, 'state' | 'suggested'>): boolean {
  return task.suggested === true && task.state === 'draft'
}

/** The section the board DRAWS a row under: `sectionOf`, with an asking Stopped row lifted to Questions and an
 *  In progress row still `ready` (queued, or waiting for a node) to Queued and a Jaah suggestion to its own section. */
export function boardSectionOf(task: PipelineTask): BoardSection {
  const section = sectionOf(task)
  if (section === 'stopped' && task.stop?.kind === 'question') return 'questions'
  if (section === 'in_progress' && task.state === 'ready') return 'queued'
  if (section === 'draft' && isSuggestion(task)) return 'suggested'
  return section
}

/** A display section's server total: Questions and Stopped split `counts.stopped` by stop kind; In
 *  progress and Queued split `counts.in_progress`; Draft and Suggested split `counts.draft`. */
export function boardSectionTotal(counts: TaskTreeCounts, section: BoardSection): number {
  if (section === 'questions') return counts.questions
  if (section === 'stopped') return counts.stopped - counts.questions
  if (section === 'queued') return counts.queued ?? 0
  if (section === 'in_progress') return counts.in_progress - (counts.queued ?? 0)
  if (section === 'suggested') return counts.suggested ?? 0
  if (section === 'draft') return (counts.draft ?? 0) - (counts.suggested ?? 0)
  return counts[section]
}

/** Stopped's make-up beside its total ("2 failed, 1 paused"), so a glance tells a fault from a pause; `undefined`
 *  for every other section and for an empty Stopped. Server counts (`task_tree._KIND_FIELD`), never the window. */
export function boardSectionNote(counts: TaskTreeCounts, section: BoardSection): string | undefined {
  if (section !== 'stopped') return undefined
  const parts = [
    counts.failed > 0 ? `${counts.failed} failed` : null,
    counts.paused > 0 ? `${counts.paused} paused` : null,
  ].filter(part => part !== null)
  return parts.length > 0 ? parts.join(', ') : undefined
}

function rankWithin(task: PipelineTask): number {
  const section = sectionOf(task)
  if (section === 'stopped') {
    const rank = task.stop ? STOP_KIND_ORDER.indexOf(task.stop.kind) : -1
    return rank < 0 ? STOP_KIND_ORDER.length : rank
  }
  if (section === 'in_progress') {
    const rank = IN_PROGRESS_STATE_ORDER.indexOf(task.state)
    return rank < 0 ? IN_PROGRESS_STATE_ORDER.length : rank
  }
  return 0
}

/** Questions → Stopped → In progress → Queued → Draft → Suggested by Jaah → Done; inside Stopped failed → paused,
 *  inside In progress running → delivering an answer. STABLE: the server's recency order holds within a rank. */
export function sortBySection(tasks: readonly PipelineTask[]): PipelineTask[] {
  return tasks
    .map((task, i) => ({ task, i }))
    .sort(
      (a, b) =>
        SECTION_ORDER.indexOf(boardSectionOf(a.task)) - SECTION_ORDER.indexOf(boardSectionOf(b.task)) ||
        rankWithin(a.task) - rankWithin(b.task) ||
        a.i - b.i,
    )
    .map(entry => entry.task)
}

type Bucket = { id: string; label: string; tasks: PipelineTask[] }

/** The Feature axis's buckets: the tree's feature order (so buckets never reshuffle as pages land), then its
 *  deleted features that still hold tasks (`deleted_features`, named so their tasks keep their group), ids the tree
 *  does not hold after them, then each project's project-level tasks (`project:<project id>`, named by its
 *  project) last, in the tree's project order. */
export function featureBuckets(tree: TaskTree | undefined, rows: readonly PipelineTask[]): Bucket[] {
  const names = new Map<string, string>()
  for (const project of tree?.projects ?? []) for (const feature of project.features) names.set(feature.id, feature.name)
  for (const project of tree?.projects ?? []) {
    for (const feature of project.deleted_features ?? []) if (!names.has(feature.id)) names.set(feature.id, feature.name)
  }
  const projects = tree?.projects ?? []
  const buckets = new Map<string, Bucket>()
  for (const task of rows) {
    const id = task.feature_id ?? `project:${task.project_id}`
    let bucket = buckets.get(id)
    if (!bucket) {
      const label = task.feature_id
        ? (names.get(task.feature_id) ?? 'Feature')
        : (projects.find(p => p.id === task.project_id)?.name ?? 'Project')
      buckets.set(id, (bucket = { id, label, tasks: [] }))
    }
    bucket.tasks.push(task)
  }
  const order = [...names.keys()]
  const rank = new Map(order.map((id, i) => [id, i]))
  const projectRank = new Map(projects.map((p, i) => [`project:${p.id}`, i]))
  // Features first, then each project's own buckets in project order (one the tree does not hold last).
  const rankOf = (id: string) =>
    id.startsWith('project:') ? order.length + 1 + (projectRank.get(id) ?? projects.length) : (rank.get(id) ?? order.length)
  return [...buckets.values()].sort((a, b) => rankOf(a.id) - rankOf(b.id))
}
