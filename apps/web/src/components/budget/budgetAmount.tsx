import type { JSX } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { BUDGETS_QUERY, putBudget, type Budget, type BudgetScope } from '../../lib/budgets'
import { dollarsToMicros } from '../../lib/jaahAi'
import type { OrgRef } from '../../types'
import FormTextField, { type FormTextFieldProps } from '../../ui/FormTextField'
import { DOLLAR_SLOTS } from '../../ui/moneyInput'

/* The ONE monthly-budget amount editor's parts, shared by the project's/group's field (`BudgetField.tsx`) and the
 * budget's own page (`pages/org/billing/BudgetEditPage.tsx`): the
 * `$` field, the dollars → micros rule (a budget is a positive amount), and the save that refreshes every budget
 * read. Before this, each editor re-spelled all three. */

/** What a budget does, on every budget amount field. */
const BUDGET_HELP = {
  tip: "New tasks wait once this month's spend reaches it.",
  more: 'Tasks already running finish. New tasks start again next month, or once the budget is raised. Near limit shows from 80% spent.',
  docs: 'administration/plan-and-billing/billing',
}

/** The `$` amount field, "Monthly budget", with what a budget does as its help; everything else is `FormTextField`'s.
 *  No placeholder: an empty field is "no budget", and a sample amount there read as a budget beside "No budget". */
export function BudgetAmountField(rest: Omit<FormTextFieldProps, 'label' | 'help' | 'slotProps'>): JSX.Element {
  return <FormTextField label="Monthly budget" help={BUDGET_HELP} slotProps={DOLLAR_SLOTS} {...rest} />
}

/** The typed dollars as micros, or null unless a valid amount above zero. */
export function budgetMicros(dollars: string): number | null {
  const micros = dollarsToMicros(dollars)
  return micros !== null && micros > 0 ? micros : null
}

/** Saves a budget, invalidates every budget read (`BUDGETS_QUERY`) and resolves with the saved budget. `org`: as
 *  another org (`BudgetField`). */
export function useBudgetSave(): (scope: BudgetScope, id: number | string, micros: number, org?: OrgRef | null) => Promise<Budget> {
  const client = useQueryClient()
  return async (scope, id, micros, org = null) => {
    const saved = await putBudget(scope, id, micros, org)
    await client.invalidateQueries({ queryKey: [BUDGETS_QUERY] })
    return saved
  }
}
