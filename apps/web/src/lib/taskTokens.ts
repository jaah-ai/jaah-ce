/* Task token totals as prose: the one compact rule and the sentence saying what the figure covers. Drawn by the
 * Activity feed's row (`components/tasks/listViews/feedRow.tsx`). Like `lib/taskCost.ts` it formats a figure
 * and never produces one: `tokens_total` already is the server's subtree sum (`types.ts` owns its meaning). */
import type { PipelineTask } from '../types'
import { compactCount, groupedCount } from './humanize'

/* A token count in a 56px cell: `compactCount`, because a long run reaches tens of millions and the exact count
   is in the title. */
export const formatTokenCount = compactCount

/** Is there a token total to show. Absent, `null` and a non-finite value are all "no claim" (never 0). */
export function hasTokens(task: Pick<PipelineTask, 'tokens_total'>): boolean {
  return typeof task.tokens_total === 'number' && Number.isFinite(task.tokens_total) && task.tokens_total >= 0
}

/** The figure (`"12.4M"`), or `''` so the host cell stays empty. */
export function tokensLabel(task: Pick<PipelineTask, 'tokens_total'>): string {
  return hasTokens(task) ? formatTokenCount(task.tokens_total as number) : ''
}

/** The cell's `title`: the exact count, its scope, and that a running task's figure trails by a few minutes. */
export function tokensTitle(task: Pick<PipelineTask, 'tokens_total'>): string {
  if (!hasTokens(task)) return ''
  const exact = groupedCount(task.tokens_total as number)
  return (
    `${exact} tokens — every class (input, cache writes, cache reads, output) for this task and everything ` +
    'under it; a running task updates every few minutes.'
  )
}
