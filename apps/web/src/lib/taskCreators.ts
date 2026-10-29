/* The `/tasks` creator filter's shared rules, drawn by `components/tasks/CreatorFilterBar.tsx` (strip)
 * and `components/tasks/TaskToolbar.tsx` (menu page): whether there is a choice, order, naming and
 * toggle live here so the two cannot disagree. React-free, for unit tests. */
import type { TaskCreator } from '../types'

/**
 * Whether there is a roster to draw at all (one person is enough: the filter area is the same for every
 * viewer). Only an empty roster draws nothing. Both surfaces hide behind it.
 */
export function rosterOffersAChoice(creators: TaskCreator[]): boolean {
  return creators.length > 0
}

/**
 * A creator's name with the caller's own row marked "(you)", spelled once for both surfaces. No identity
 * yet, or no row for the caller: plain names.
 */
export function creatorLabel(creator: TaskCreator, currentUserId: string | null): string {
  return creator.id === currentUserId ? `${creator.name} (you)` : creator.name
}

/**
 * Current user first, then the server's order. A pure move: `GET /api/tasks/creators` guarantees the
 * caller a row (labelled by the server's one derivation, so its initials match every other row's — see
 * `routes/auth.py`'s `AdminOut`), so nothing is synthesized. Keeps the operator out of the strip's `+N` overflow.
 */
export function orderCreators(creators: TaskCreator[], currentUserId: string | null): TaskCreator[] {
  if (!currentUserId) return creators
  const mine = creators.find(creator => creator.id === currentUserId)
  if (!mine) return creators
  return [mine, ...creators.filter(creator => creator.id !== currentUserId)]
}

/**
 * The header strip's split into drawn badges and the `+N` overflow. WHO gets one of the `cap` badges is
 * decided in priority order — the current user, then the PICKED creators, then everyone else (roster order,
 * the server's: task count, then name) — so a pick stays in view rather than lost to the `+N`. The drawn
 * badges then keep ROSTER order (the caller first): pressing a badge never moves it out from under the
 * pointer. The overflow keeps roster order too.
 */
export function splitCreatorStrip(
  creators: TaskCreator[],
  currentUserId: string | null,
  selectedIds: string[],
  cap: number,
): { shown: TaskCreator[]; overflow: TaskCreator[] } {
  const picked = new Set(selectedIds)
  const rank = (creator: TaskCreator) => (creator.id === currentUserId ? 0 : picked.has(creator.id) ? 1 : 2)
  // `sort` is stable, so each priority run keeps roster order.
  const drawn = new Set([...creators].sort((a, b) => rank(a) - rank(b)).slice(0, cap))
  const byRoster = orderCreators(creators, currentUserId)
  return {
    shown: byRoster.filter(creator => drawn.has(creator)),
    overflow: byRoster.filter(creator => !drawn.has(creator)),
  }
}

/**
 * Toggle one creator (multi-select; empty = unfiltered). The result is in roster order, not click order,
 * since it rides into the URL and `sessionStorage`. Ids not in the roster are dropped (stale restores).
 */
export function toggleCreatorId(
  selectedIds: string[],
  id: string,
  roster: TaskCreator[],
): string[] {
  const picked = new Set(selectedIds)
  if (picked.has(id)) picked.delete(id)
  else picked.add(id)
  return roster.filter(creator => picked.has(creator.id)).map(creator => creator.id)
}
