/* The `/tasks` assignee filter's shared rules, drawn by `components/tasks/AssigneeFilterBar.tsx` (strip)
 * and `components/tasks/TaskToolbar.tsx` (menu page): whether there is a choice, order, naming and
 * toggle live here so the two cannot disagree. React-free, for unit tests. */
import type { TaskAssignee } from '../types'

/**
 * Whether there is a roster to draw at all (one person is enough: the filter area is the same for every
 * viewer). Only an empty roster draws nothing. Both surfaces hide behind it.
 */
export function rosterOffersAChoice(assignees: TaskAssignee[]): boolean {
  return assignees.length > 0
}

/**
 * An assignee's name with the caller's own row marked "(you)", spelled once for both surfaces. No identity
 * yet, or no row for the caller: plain names.
 */
export function assigneeLabel(assignee: TaskAssignee, currentUserId: string | null): string {
  return assignee.id === currentUserId ? `${assignee.name} (you)` : assignee.name
}

/**
 * Current user first, then the server's order. A pure move: `GET /api/tasks/assignees` guarantees the
 * caller a row (labelled by the server's one derivation, so its initials match every other row's — see
 * `routes/auth.py`'s `AdminOut`), so nothing is synthesized. Keeps the operator out of the strip's `+N` overflow.
 */
export function orderAssignees(assignees: TaskAssignee[], currentUserId: string | null): TaskAssignee[] {
  if (!currentUserId) return assignees
  const mine = assignees.find(assignee => assignee.id === currentUserId)
  if (!mine) return assignees
  return [mine, ...assignees.filter(assignee => assignee.id !== currentUserId)]
}

/**
 * The header strip's split into drawn badges and the `+N` overflow. WHO gets one of the `cap` badges is
 * decided in priority order — the current user, then the PICKED assignees, then everyone else (roster order,
 * the server's: task count, then name) — so a pick stays in view rather than lost to the `+N`. The drawn
 * badges then keep ROSTER order (the caller first): pressing a badge never moves it out from under the
 * pointer. The overflow keeps roster order too.
 */
export function splitAssigneeStrip(
  assignees: TaskAssignee[],
  currentUserId: string | null,
  selectedIds: string[],
  cap: number,
): { shown: TaskAssignee[]; overflow: TaskAssignee[] } {
  const picked = new Set(selectedIds)
  const rank = (assignee: TaskAssignee) => (assignee.id === currentUserId ? 0 : picked.has(assignee.id) ? 1 : 2)
  // `sort` is stable, so each priority run keeps roster order.
  const drawn = new Set([...assignees].sort((a, b) => rank(a) - rank(b)).slice(0, cap))
  const byRoster = orderAssignees(assignees, currentUserId)
  return {
    shown: byRoster.filter(assignee => drawn.has(assignee)),
    overflow: byRoster.filter(assignee => !drawn.has(assignee)),
  }
}

/**
 * Toggle one assignee (multi-select; empty = unfiltered). The result is in roster order, not click order,
 * since it rides into the URL and `sessionStorage`. Ids not in the roster are dropped (stale restores).
 */
export function toggleAssigneeId(
  selectedIds: string[],
  id: string,
  roster: TaskAssignee[],
): string[] {
  const picked = new Set(selectedIds)
  if (picked.has(id)) picked.delete(id)
  else picked.add(id)
  return roster.filter(assignee => picked.has(assignee.id)).map(assignee => assignee.id)
}
