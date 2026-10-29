/* Task cost as prose: the rounding rules and the presence test. Drawn by `components/tasks/CostBadge.tsx`
 * (the task list: whole dollars from $1, cents below, exact on hover) and `components/tasks/TaskRunMetrics.tsx` (exact), which
 * show the same task's cost two clicks apart — so both rules live here.
 * No summing helper on purpose: `cost_usd` already is the server's Decimal subtree total; this file
 * formats a cost and never produces one. */
import type { PipelineTask } from '../types'

/* USD across fractions of a cent to hundreds of dollars:
     · ≥ $1   → 2 decimals ($1,204.50).
     · < $1   → 2–4 decimals, trailing zeros trimmed past the second ($0.03, $0.0042) — never `$0.00`.
     · 0 < x < $0.0001 → "< $0.0001".
   A measured `0` prints `$0.00`; nothing-to-price is `cost_status: 'unavailable'` instead. */
export function formatCostUsd(amount: number): string {
  if (amount > 0 && amount < 0.0001) return '< $0.0001'
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: amount < 1 ? 4 : 2,
  }).format(amount)
}

/* Wire value as a number, or `null` for a don't-know. Private, to prevent client-side arithmetic.
   Unparseable is a don't-know, not zero: absence is not a state.
   Blank is guarded because `Number('')` is 0; `Number.isFinite` also rejects `Infinity`. */
function parseCostUsd(cost: string | null | undefined): number | null {
  if (typeof cost !== 'string' || cost.trim() === '') return null
  const parsed = Number(cost)
  return Number.isFinite(parsed) ? parsed : null
}

/**
 * Is there a cost to show. False for `cost_status: 'unavailable'`, absent fields (see `PipelineTask`),
 * or an unparseable `cost_usd` — callers must not tell these apart. `'partial'` is a real figure (a lower
 * bound). Takes a `Pick` so narrower row types fit without a cast.
 */
export function hasCost(task: Pick<PipelineTask, 'cost_usd' | 'cost_status'>): boolean {
  if (task.cost_status === 'unavailable') return false
  return parseCostUsd(task.cost_usd) !== null
}

/* The task list's glanceable figure: whole US dollars, ROUNDED UP, from $1, so `$12.34` reads
   `$13`; below $1, cents, so a `$0.05` run no longer reads `$1`. Never `$0` or `$0.00` for a real
   cost: a positive spend under a cent reads `< $0.01`. The exact figure is one hover away in `costTitle`. */
function formatListUsd(amount: number): string {
  if (amount > 0 && amount < 0.01) return '< $0.01'
  const cents = amount > 0 && amount < 1
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    /* Both bounds: USD's default minimum is 2, and older engines throw a RangeError when max < min. */
    minimumFractionDigits: cents ? 2 : 0,
    maximumFractionDigits: cents ? 2 : 0,
  }).format(cents ? amount : Math.ceil(amount) || 0) // `|| 0` folds `Math.ceil(-0.4)`'s `-0`, which would print `-$0`.
}

/**
 * The list figure (`"$13"` for 12.34, `"$0.42"` for 0.42), or `''` — the caller picks the placeholder.
 * Load-bearing: the `/tasks` table gates its Cost column on `:not(:empty)`, so `CostBadge` renders `null`
 * for `''`.
 */
export function costLabel(task: Pick<PipelineTask, 'cost_usd' | 'cost_status'>): string {
  if (!hasCost(task)) return ''
  return formatListUsd(parseCostUsd(task.cost_usd) as number)
}

/**
 * The badge's hover `title`: the exact figure (`formatCostUsd`), and whether it includes a cost the agent
 * reported itself (`cost_agent_reported`, Efficiency's `agent-reported`). `''` with no cost, so no empty
 * `title` reaches the DOM.
 */
export function costTitle(
  task: Pick<PipelineTask, 'cost_usd' | 'cost_status' | 'cost_agent_reported'>,
): string {
  if (!hasCost(task)) return ''
  const exact = formatCostUsd(parseCostUsd(task.cost_usd) as number)
  return task.cost_agent_reported === true ? `${exact} · includes agent-reported cost` : exact
}

/* The trigger budget flag's sentence on the Task detail (R11, advisory): "Over trigger budget: $0.42 of $0.01". */
export function triggerBudgetText(cost: number, budget: number | null): string {
  return budget == null
    ? `Over trigger budget: ${formatCostUsd(cost)}`
    : `Over trigger budget: ${formatCostUsd(cost)} of ${formatCostUsd(budget)}`
}
