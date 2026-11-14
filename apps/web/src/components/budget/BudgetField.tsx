import { useState, type JSX } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import { grid } from '../../theme'
import Button from '@mui/material/Button'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { apiDetail } from '../../api'
import { useAuth } from '../../auth'
import { useSubmit } from '../../hooks/useSubmit'
import { BUDGETS_QUERY, budgetKey, deleteBudget, getBudget, type BudgetReading, type BudgetScope } from '../../lib/budgets'
import { microsText, microsToDollarsInput } from '../../lib/jaahAi'
import { isOrgAdminSeat } from '../../lib/orgAdmin'
import { usePlanSwitch } from '../../lib/planSwitches'
import type { OrgRef } from '../../types'
import Banner from '../../ui/Banner'
import FormBox from '../../ui/FormBox'
import { tapTargetSx } from '../../ui/listStyles'
import LoadingState from '../../ui/LoadingState'
import StatusChip from '../../ui/StatusChip'
import SubmitButton from '../../ui/SubmitButton'
import { BudgetAmountField, budgetMicros, useBudgetSave } from './budgetAmount'

/* A project's or a group's monthly budget (`lib/budgets.ts`), saved on its own — never part of the page's form.
 * An Org Owner/Admin's only (the routes are `@org_admin`): anyone else gets nothing. With the plan's switch off,
 * nothing is shown and nothing fetched (every route would 403). `org`: another org's scope (an operator's),
 * read and written as that org for each request (`lib/actAs.ts::readAsOnce`, `writeAsOnce`). */

type BudgetFieldProps = { scope: BudgetScope; scopeId: number | string; org?: OrgRef | null }

export function BudgetField({ scope, scopeId, org = null }: BudgetFieldProps): JSX.Element | null {
  const { admin } = useAuth()
  const on = usePlanSwitch('budgets_enabled')
  if (!isOrgAdminSeat(admin)) return null
  // The viewer's plan, for its own scope only: another org's plan is that org's, and its 403 says so.
  if (!on && org === null) return null
  return <BudgetForm scope={scope} scopeId={scopeId} org={org} />
}

/** "Spent this month: $3.20 of $10.00 (32%)", or "… · No budget" while unlimited. */
export function spentText(reading: BudgetReading): string {
  const { budget } = reading
  if (!budget) return `Spent this month: ${microsText(reading.spent_micros)} · No budget`
  return `Spent this month: ${microsText(budget.spent_micros)} of ${microsText(budget.monthly_usd_micros)} (${budget.percent}%)`
}

function BudgetForm({ scope, scopeId, org }: BudgetFieldProps): JSX.Element {
  const client = useQueryClient()
  const asOrg = org ?? null
  const query = useQuery({ queryKey: budgetKey(scope, scopeId), queryFn: () => getBudget(scope, scopeId, asOrg) })
  const stored = query.data?.budget?.monthly_usd_micros ?? null
  const storedText = stored === null ? '' : microsToDollarsInput(stored)
  const [draft, setDraft] = useState(storedText)
  // Re-seed the draft in the same render the stored amount changes (React's "adjust state on a prop change"),
  // never in an effect: an effect commits one frame with the read's figures beside an EMPTY amount.
  const [seeded, setSeeded] = useState(stored)
  if (seeded !== stored) {
    setSeeded(stored)
    setDraft(storedText)
  }
  const { busy, formError, run, saved, clearSaved } = useSubmit()
  const saveBudget = useBudgetSave()

  if (query.isPending) return <LoadingState rows={1} />
  if (query.isError) return <Banner severity="error">{apiDetail(query.error, 'Could not load the budget')}</Banner>

  const reading = query.data
  const budget = reading.budget
  const micros = budgetMicros(draft)
  const changed = micros !== stored

  async function save() {
    if (micros === null) return
    await run('Could not save the budget', () => saveBudget(scope, scopeId, micros, asOrg))
  }

  async function remove() {
    await run('Could not remove the budget', async () => {
      await deleteBudget(scope, scopeId, asOrg)
      await client.invalidateQueries({ queryKey: [BUDGETS_QUERY] })
    })
  }

  return (
    <Stack spacing={1.5} data-testid="budget-field" data-scope={scope}>
      {budget?.state === 'held' && (
        <Banner severity="warning" testId="budget-held">Budget reached — new tasks wait.</Banner>
      )}
      <FormBox onSubmit={save} sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'flex-start', gap: 1 }}>
        <Box sx={{ flex: `1 1 ${grid(32)}`, minWidth: 0, maxWidth: grid(46.5) }}>
          <BudgetAmountField
            error={formError || undefined}
            hint={saved ? 'Saved.' : undefined}
            value={draft}
            onChange={v => {
              setDraft(v)
              clearSaved()
            }}
            disabled={busy}
            testId="budget-amount"
          />
        </Box>
        <Box sx={{ display: 'flex', gap: 1, flexShrink: 0 }}>
          <SubmitButton variant="outlined" busy={busy} disabled={!changed || micros === null} testId="budget-save">
            Save
          </SubmitButton>
          {budget && (
            <Button
              type="button"
              color="error"
              disabled={busy}
              onClick={() => void remove()}
              data-testid="budget-remove"
              sx={tapTargetSx}
            >
              Remove
            </Button>
          )}
        </Box>
      </FormBox>
      <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 1 }}>
        <Typography
          variant="body2"
          data-testid="budget-spent"
          sx={{ color: 'text.secondary', fontVariantNumeric: 'tabular-nums', overflowWrap: 'anywhere' }}
        >
          {spentText(reading)}
        </Typography>
        {budget?.state === 'warn' && <StatusChip label="Near limit" tone="warning" />}
      </Box>
    </Stack>
  )
}

export default BudgetField
