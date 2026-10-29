/* The `/tasks` board's Group-by vocabulary: the one home for what a `groupBy` value means (`''`, a
 * built-in sentinel, or a tag-group UUID), how the URL spells it (`?group=`), and which values a scope
 * offers. Pure of React.
 */


/** No sectioning at all: one flat list, the board's own unfinished-first order. */
export const GROUP_BY_NONE = ''

/** Section by task state — Stopped / In progress / Queued / Draft / Done (D6-state). The board's default. Reserved word:
 *  every real group id is a server-minted UUID, so it cannot collide. */
export const GROUP_BY_STATE = 'state'

/** Section by feature (plus "Other"). Reserved word, as above. */
export const GROUP_BY_FEATURE = 'feature'

/** Section by when each row last moved — the Activity Feed's recency buckets. Reserved word, as above. */
export const GROUP_BY_RECENCY = 'recency'

/** Section by PROJECT (the repo, `tasks.project_id`). Offered only when the selection spans more than one
 *  project, i.e. under All projects (`offeredGroupBys`). Reserved word, as above. */
export const GROUP_BY_PROJECT = 'project'

/** The board's default, and what an absent `?group=` means. */
export const DEFAULT_GROUP_BY: string = GROUP_BY_STATE

export function isRecencyGroupBy(groupBy: string): boolean {
  return groupBy === GROUP_BY_RECENCY
}

/** Is the board sectioned by project (the repo)? */
export function isProjectGroupBy(groupBy: string): boolean {
  return groupBy === GROUP_BY_PROJECT
}

/** Is the board sectioned by feature ("Other" last)? */
export function isFeatureGroupBy(groupBy: string): boolean {
  return groupBy === GROUP_BY_FEATURE
}

const BUILT_INS = new Set([GROUP_BY_NONE, GROUP_BY_STATE, GROUP_BY_FEATURE, GROUP_BY_RECENCY, GROUP_BY_PROJECT])

/**
 * Does this value name a tag group the catalog could confirm? Every built-in is subtracted here: a sentinel
 * read as a tag reference would be judged dead against the catalog and fall back to the default.
 */
export function isTagGroupBy(groupBy: string): boolean {
  return !BUILT_INS.has(groupBy)
}

/* ── The URL spelling (`?group=`) ───────────────────────────────────────────────────────────────────── */

/** The `/tasks` query param carrying the Group-by choice. Absent means `DEFAULT_GROUP_BY`. */
export const GROUP_PARAM = 'group'

const NONE_PARAM = 'none'
const TAG_PREFIX = 'tag:'

/** A raw `?group=` as a `groupBy`. TOTAL: absent or junk is the default; `tag:<id>` is that tag group (its
 *  existence is the catalog's to judge). */
export function parseGroupParam(raw: string | null): string {
  if (raw === null) return DEFAULT_GROUP_BY
  if (raw === NONE_PARAM) return GROUP_BY_NONE
  if (raw.startsWith(TAG_PREFIX) && raw.length > TAG_PREFIX.length) return raw.slice(TAG_PREFIX.length)
  if (raw !== GROUP_BY_NONE && BUILT_INS.has(raw)) return raw
  return DEFAULT_GROUP_BY
}

/** The `?group=` value for `groupBy`, or `null` for the default (the param is then left out). */
export function groupParamValue(groupBy: string): string | null {
  if (groupBy === DEFAULT_GROUP_BY) return null
  if (groupBy === GROUP_BY_NONE) return NONE_PARAM
  return isTagGroupBy(groupBy) ? `${TAG_PREFIX}${groupBy}` : groupBy
}

/* ── Which values a selection offers ────────────────────────────────────────────────────────────────── */

export type GroupByOption = { value: string; label: string; hint: string }

/**
 * The Group-by options for a selection, in menu order: State (default), Feature, Recency, Project (only
 * when the selection spans several projects), each ACTIVE tag group, None.
 */
export function offeredGroupBys(
  spansProjects: boolean,
  tagGroups: ReadonlyArray<{ id: string; name: string; state?: string }>,
): GroupByOption[] {
  return [
    { value: GROUP_BY_STATE, label: 'State', hint: 'Stopped, In progress, Queued, Draft, Done.' },
    { value: GROUP_BY_FEATURE, label: 'Feature', hint: 'One section per feature, plus one per project for tasks with none.' },
    { value: GROUP_BY_RECENCY, label: 'Recency', hint: 'Running now, Last hour, Today, This week, Older.' },
    ...(spansProjects
      ? [{ value: GROUP_BY_PROJECT, label: 'Project', hint: 'One section per project.' }]
      : []),
    ...tagGroups
      .filter(group => group.state === undefined || group.state === 'active')
      .map(group => ({
        value: group.id,
        label: group.name,
        hint: `${group.name} — sections follow your tag order, so a finished section can sit above a running one.`,
      })),
    { value: GROUP_BY_NONE, label: 'None', hint: 'One list, unfinished first.' },
  ]
}

/** The value the board actually groups by: `groupBy` when the selection offers it, else the default. A
 *  tag group is judged only once the catalog has answered (`catalogAnswered`), so a loading catalog never
 *  flips a deep link to State. */
export function resolveGroupBy(
  groupBy: string,
  options: readonly GroupByOption[],
  catalogAnswered: boolean,
): string {
  if (options.some(option => option.value === groupBy)) return groupBy
  if (isTagGroupBy(groupBy) && !catalogAnswered) return groupBy
  return DEFAULT_GROUP_BY
}
