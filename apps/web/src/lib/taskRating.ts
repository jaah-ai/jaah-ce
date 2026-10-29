/* The task rating's rules and prose: which tasks may be rated, and the strings a rating reduces to.
 * `components/tasks/RatingBadge.tsx` and `components/tasks/TaskRating.tsx` draw them. Pure of React so the
 * rounding and pluralisation are unit-tested (`taskRating.test.ts`).
 * No averaging function by design: the server's PUT answers the recomputed aggregate (see `types.ts`'s
 * `TaskRating` block), and a client mean over a partial vote set would disagree with the board. This
 * file FORMATS an average, never produces one. */
import type { PipelineTask, TaskRatingVote } from '../types'

/**
 * May this task be rated? One predicate for every surface that OFFERS a rating
 * (`pages/taskDetail/useTaskDetailTabs.ts`, `components/tasks/listViews/rows.ts`).
 *
 * Done AND (top-level OR an epic): a rating judges a deliverable somebody asked for, not an internal
 * pipeline stage (`business_analysis`/`architecture`/`plan`/`coder`/`finish` children).
 * Presentation only: the server 409s a vote on a non-ratable task.
 * All three disjuncts stay: an epic nested under another row must still be ratable.
 * `parent_task_id == null` is loose so absent and null read alike.
 * The epic half mirrors `components/tasks/shared.tsx:doneWarnReason` (`persona === 'epic' ||
 * is_plannable`); if that gains an arm, this must too.
 * A `Pick`, so narrower list-view rows satisfy it without a cast.
 */
export function isRatableTask(
  task: Pick<PipelineTask, 'state' | 'parent_task_id' | 'persona' | 'is_plannable'>,
): boolean {
  if (task.state !== 'done') return false
  return task.parent_task_id == null || task.persona === 'epic' || task.is_plannable
}

/* Rendering rule: HALF UP at one decimal (`Math.round(avg * 10) / 10`), then drop a trailing `.0`:
 * `4.5` → "4.5", `5` → "5", `4.25` → "4.3", `4.96` → "5". `Math.round`, not `toFixed(1)`: `toFixed`
 * rounds the decimal string of a binary double (`(8.475).toFixed(2) === '8.47'`), which would bite at
 * any other precision. */
/* The server is the arithmetic authority: `apps/api/app/task_ratings.py::average_of` divides and rounds
 * half-to-EVEN; the wire value is already one decimal, so re-rounding is the identity. Never hand this a
 * raw mean. */
export function formatAverage(avg: number | null): string {
  // `''`: an absent average is the caller's placeholder to choose. Non-finite folds in too — it crosses the
  // wire, and `NaN` would render literally.
  if (avg === null || !Number.isFinite(avg)) return ''
  const rounded = Math.round(avg * 10) / 10
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1)
}

/**
 * The badge's `title`: "4.5 out of 5 — 2 ratings". The denominator and count are spelled out because the
 * badge (one star, one number) cannot show them. `average` is the authority: a positive count with a null
 * average reads as nothing to show.
 */
export function ratingSummaryTitle(count: number, average: number | null): string {
  const shown = formatAverage(average)
  // Also the `count === 0` case: every done task until somebody votes.
  if (shown === '' || !Number.isFinite(count) || count <= 0) return 'Not rated yet'
  return `${shown} out of 5 — ${count} ${count === 1 ? 'rating' : 'ratings'}`
}

/**
 * Per-voter breakdown, one per line: `"Grace Hopper — 5 stars\nAda Lovelace — 4 stars"`.
 * One STRING because MUI's `Tooltip` (with `describeChild`) also writes it to the child's native `title`
 * while closed, so it survives for screen readers, touch and jsdom tests. "5 stars", not "5★": screen
 * readers mangle U+2605. `''` for no votes.
 */
export function voteListTitle(votes: TaskRatingVote[]): string {
  if (!Array.isArray(votes) || votes.length === 0) return ''
  return votes
    .map(vote => {
      /* Defensive arm for an unresolved name (the server sends resolved ones), so a line never opens with
       * the separator. */
      const who = typeof vote.user === 'string' && vote.user.trim() !== '' ? vote.user.trim() : 'Unknown'
      const stars = Number.isFinite(vote.stars) ? vote.stars : 0
      return `${who} — ${stars} ${stars === 1 ? 'star' : 'stars'}`
    })
    .join('\n')
}
