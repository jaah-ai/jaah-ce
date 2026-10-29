import type { PipelineTask } from '../../types'

// The `/tasks` board's GROUP BY sections — which section each top-level row belongs to, on either axis. PURE and
// DOM-free; shared by every renderer that sections the board (`listViews/ActivityFeedView.tsx`), so they agree.

/* GROUP BY — everything the flattener needs to section the board. Two axes, discriminated on `axis`.
   ── `axis: 'tag'` — one section per tag of one tag group ──────────────────────────────────────────
   `tags` is the group's own catalog in `position` order (not alphabetical), passed in rather than
   collected from rows: sections derived from the loaded window would change as you scroll, and an empty
   tag would vanish. Every tag gets a section. `counts` are server totals (`GET /api/tasks/tag-counts`),
   zero-filled; `untagged` is that endpoint's global figure; a missing count renders no number.
   `isCollapsed` is a host-owned predicate keyed by SECTION ID above the list (never by index or child
   state, which re-open on scroll). `''` asks about the fallback (Untagged / No project).
   ── `axis: 'project'` — one section per PROJECT, the repo (`lib/taskGrouping.ts`) ─────────────────
   `projects` is the task tree's roster (`GET /api/tasks/tree`, server order), each id the project's id as a
   string. Differences:
     · No server counts: `total: null`.
     · Empty sections are DROPPED (the roster is every visible project, not a curated vocabulary).
     · Exactly one section per row (`tasks.project_id` is NOT NULL, single-valued): no repeats.
   Rows off the roster fall into a trailing `''` "No project" section, shown only with members — kept so the
   partition always COVERS the rows (a task the roster cannot name stays on the board, unnamed). */
export type TaskTreeGrouping =
  | {
      axis: 'tag'
      tags: readonly { id: string; name: string }[]
      counts: Record<string, number>
      untagged: number | null
      isCollapsed: (sectionId: string) => boolean
    }
  | {
      axis: 'project'
      projects: readonly { id: string; name: string }[]
      isCollapsed: (sectionId: string) => boolean
    }

/** The two arms, named, so a caller meaning one axis says which without spelling `Extract<>`. */
export type TagAxisGrouping = Extract<TaskTreeGrouping, { axis: 'tag' }>
export type ProjectAxisGrouping = Extract<TaskTreeGrouping, { axis: 'project' }>

/* ── One membership rule for a tag group's sections ────────────────────────────────────────────────
   Generic over the item (the caller passes an accessor).
     · One section per tag in the group's `position` order (server order, never re-sorted).
     · Membership is the TOP-LEVEL row's; a tagged child travels with its parent.
     · A row with several of the group's tags appears under EACH; `repeat` marks later appearances.
     · Untagged is LAST and means "no tag from THIS group". */
/** One section of the board, on either axis. `tagId` is a tag id or a PROJECT id; the name is kept
 *  because it keys the whole fold path (`data-section-tag`, `onToggleSection`, the host's collapse map). Both axes mint UUIDs, so no collision. Renaming is a deferred follow-up. */
export type TagSection<T> = {
  /** `''` for the trailing fallback section — Untagged on the tag axis, No project on the project one. */
  tagId: string
  label: string
  members: T[]
}

export function splitByTagGroup<T>(
  items: T[],
  taskOf: (item: T) => PipelineTask,
  tags: readonly { id: string; name: string }[],
): TagSection<T>[] {
  const groupTagIds = new Set(tags.map(tag => tag.id))
  const hasTag = (item: T, tagId: string) =>
    (taskOf(item).tags ?? []).some(ref => ref.id === tagId)
  const sections: TagSection<T>[] = tags.map(tag => ({
    tagId: tag.id,
    label: tag.name,
    members: items.filter(item => hasTag(item, tag.id)),
  }))
  sections.push({
    tagId: '',
    label: 'Untagged',
    members: items.filter(item => !(taskOf(item).tags ?? []).some(ref => groupTagIds.has(ref.id))),
  })
  return sections
}

/* ── One section total, whichever axis is on ───────────────────────────────────────────────────────
   Tag sections have the server's figure (`GET /api/tasks/tag-counts`); project sections none (no route
   counts per project), so `null` rather than a misleading window count. */
export function sectionTotal(grouping: TaskTreeGrouping, sectionId: string): number | null {
  if (grouping.axis === 'project') return null
  // A TYPE CHECK, NOT A KEY CHECK: `counts` is a parsed object with a prototype (`'toString' in counts`).
  // Untagged's total counts rows with no tag in ANY active group — an UNDER-count when a second group is active.
  if (sectionId === '') return grouping.untagged
  return typeof grouping.counts[sectionId] === 'number' ? grouping.counts[sectionId] : null
}

/* ── Which sections the board has, on either axis — the one entry point every renderer calls. */
export function splitBoardSections<T>(
  grouping: TaskTreeGrouping,
  items: T[],
  taskOf: (item: T) => PipelineTask,
): TagSection<T>[] {
  return grouping.axis === 'project'
    ? splitByProject(items, taskOf, grouping.projects)
    : splitByTagGroup(items, taskOf, grouping.tags)
}

/* ── One membership rule for the project axis ──────────────────────────────────────────────────────
   One section per PROJECT in roster order (`GET /api/tasks/tree`), never re-sorted. Empty sections are
   DROPPED (see `TaskTreeGrouping`). The trailing `''` "No project" section appears only with members.
   DO NOT DELETE THAT ARM AS DEAD CODE: it is reachable by design (see `TaskTreeGrouping`), and without it
   the partition would not COVER `items`, silently dropping rows. Pinned by `taskTree.test.ts` ("keeps a row
   whose project the roster withholds"). `project_id` is single-valued: no repeats. */
export function splitByProject<T>(
  items: T[],
  taskOf: (item: T) => PipelineTask,
  projects: readonly { id: string; name: string }[],
): TagSection<T>[] {
  const sections: TagSection<T>[] = []
  const known = new Set(projects.map(project => project.id))
  for (const project of projects) {
    const members = items.filter(item => String(taskOf(item).project_id) === project.id)
    if (members.length > 0) sections.push({ tagId: project.id, label: project.name, members })
  }
  const orphans = items.filter(item => !known.has(String(taskOf(item).project_id)))
  if (orphans.length > 0) sections.push({ tagId: '', label: 'No project', members: orphans })
  return sections
}
