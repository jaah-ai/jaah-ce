// Monthly budgets on a project or a group (`routes/budgets.py`): Jaah AI model spend this UTC calendar month.
// At 80% admins are warned; at 100% new tasks wait until a raise or the month rolls over. No budget = unlimited.
// Org Owner/Admin only; every route answers 403 while the plan's `budgets_enabled` switch is off.
// Money on the wire is integer micro-USD (1 USD = 1_000_000). React-free.

import { del, get, put } from '../api'
import { readAsOnce, writeAsOnce } from './actAs'
import type { OrgRef } from '../types'

export type BudgetScope = 'project' | 'group'
/** `ok` < 80%, `warn` 80–99%, `held` ≥ 100% (new tasks wait). */
export type BudgetState = 'ok' | 'warn' | 'held'

export type Budget = {
  id: string
  scope: BudgetScope
  project_id: number | null
  group_id: string | null
  name: string
  monthly_usd_micros: number
  spent_micros: number
  /** Whole percent, floored. */
  percent: number
  state: BudgetState
  /** The first day of the UTC month, ISO date. */
  month: string
  updated_at: string
  updated_by: string | null
}

/** One scope's reading: its budget (null = unlimited) and this month's spend either way. */
export type BudgetReading = { budget: Budget | null; spent_micros: number; month: string }

/** Query keys. Each extends the root, so invalidating the root refreshes every budget read. */
export const BUDGETS_QUERY = 'budgets'
export const BUDGETS_LIST_KEY = [BUDGETS_QUERY, 'list'] as const
export const budgetKey = (scope: BudgetScope, id: number | string) => [BUDGETS_QUERY, scope, String(id)] as const

const path = (scope: BudgetScope, id: number | string) => `/api/budgets/${scope}/${encodeURIComponent(String(id))}`

export const listBudgets = () => get<{ items: Budget[] }>('/api/budgets')
/* `org`: another org's scope, read and written as that org (`lib/actAs.ts::readAsOnce`). */
export const getBudget = (scope: BudgetScope, id: number | string, org: OrgRef | null = null) =>
  get<BudgetReading>(path(scope, id), ...readAsOnce(org))
/** 422 with a `detail` sentence on a bad amount. */
export const putBudget = (scope: BudgetScope, id: number | string, monthly_usd_micros: number, org: OrgRef | null = null) =>
  put<Budget>(path(scope, id), { monthly_usd_micros }, ...writeAsOnce(org))
export const deleteBudget = (scope: BudgetScope, id: number | string, org: OrgRef | null = null) =>
  del(path(scope, id), ...writeAsOnce(org))
