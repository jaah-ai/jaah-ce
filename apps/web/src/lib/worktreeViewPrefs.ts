import { readJson, readString, validEntries, writeJson, writeString } from './safeStorage'
import { DEFAULT_WORKTREE_GROUP_BY, defaultGroupCollapsed, isWorktreeGroupBy, type WorktreeGroupBy } from './worktreeGrouping'

/* ── The Worktrees page's two view preferences, and why neither is in the URL ──────────────────────
 * `lib/worktreeFilters.ts` owns what REMOVES ROWS, in the query string (a filtered list is sent to
 * someone). Grouping and folds are the READER's, so a link must not carry them.
 * `localStorage`, not `sessionStorage` (the opposite of `lib/taskFilterStorage.ts`, whose argument is
 * about filters): re-folding every group each visit would be wrong for a long fleet-wide pool.
 * NEITHER READ CAN THROW: both run in the first render, and a bad value must give the default view.
 */

/** The grouping the operator last chose. */
const GROUP_BY_KEY = 'cockpit.worktrees.groupBy'

/** The groups they have OPENED, and those they have FOLDED. See `readGroupFolds` for why both. */
const EXPANDED_GROUPS_KEY = 'cockpit.worktrees.expandedGroups'
const FOLDED_GROUPS_KEY = 'cockpit.worktrees.foldedGroups'

/** The grouping to open on: the operator's last choice, else `project → node`. */
export function readGroupBy(): WorktreeGroupBy {
  const raw = readString('local', GROUP_BY_KEY)
  return raw !== null && isWorktreeGroupBy(raw) ? raw : DEFAULT_WORKTREE_GROUP_BY
}

/** Losing a preference is not worth an error boundary. */
export function writeGroupBy(groupBy: WorktreeGroupBy): void {
  writeString('local', GROUP_BY_KEY, groupBy)
}

/* ── Openings AND folds ────────────────────────────────────────────────────────────────────────────
 * A group's default depends on its size (`defaultGroupCollapsed`): a small group opens, a large one folds.
 * So neither sense alone can record every choice — folding a small group is a fold, opening a large one an
 * opening — and a group in neither set takes its default. A key in both cannot arise (`withGroupFold` moves
 * it); were one stored, the opening wins.
 */
export type GroupFolds = { readonly opened: ReadonlySet<string>; readonly folded: ReadonlySet<string> }

function readKeySet(key: string): Set<string> {
  return new Set(validEntries(readJson('local', key), (k): k is string => typeof k === 'string'))
}

export function readGroupFolds(): GroupFolds {
  return { opened: readKeySet(EXPANDED_GROUPS_KEY), folded: readKeySet(FOLDED_GROUPS_KEY) }
}

export function writeGroupFolds(folds: GroupFolds): void {
  // Same as above: a preference that cannot be stored is still honoured for this visit.
  writeJson('local', EXPANDED_GROUPS_KEY, [...folds.opened])
  writeJson('local', FOLDED_GROUPS_KEY, [...folds.folded])
}

/** The folds with one group set to `collapsed`, explicitly (it no longer follows its size default). */
export function withGroupFold(folds: GroupFolds, groupKey: string, collapsed: boolean): GroupFolds {
  const opened = new Set(folds.opened)
  const folded = new Set(folds.folded)
  if (collapsed) {
    opened.delete(groupKey)
    folded.add(groupKey)
  } else {
    folded.delete(groupKey)
    opened.add(groupKey)
  }
  return { opened, folded }
}

/** Is this group folded? The stored choice, else the size default. */
export function groupIsCollapsed(folds: GroupFolds, groupKey: string, slotCount: number): boolean {
  if (folds.opened.has(groupKey)) return false
  if (folds.folded.has(groupKey)) return true
  return defaultGroupCollapsed(slotCount)
}
