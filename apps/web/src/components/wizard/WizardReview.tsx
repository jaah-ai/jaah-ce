import type { JSX, ReactNode } from 'react'
import Box from '@mui/material/Box'
import Link from '@mui/material/Link'
import { KeyValue, KeyValueList } from '../../ui/KeyValueList'

/* The wizard's review step (module contract: `useWizard.ts`): every chosen value read-only, rows grouped by
 * the step that set them, ONE Edit link per step. Edit calls `onEdit(stepId)`; the consumer calls `goTo`. */

export type WizardReviewRow = { stepId: string; label: string; value: ReactNode }

/** One group per step, in the order each step first appears in `rows`. */
function groupByStep(rows: readonly WizardReviewRow[]): WizardReviewRow[][] {
  const groups = new Map<string, WizardReviewRow[]>()
  for (const row of rows) groups.set(row.stepId, [...(groups.get(row.stepId) ?? []), row])
  return [...groups.values()]
}

export function WizardReview({
  rows,
  onEdit,
  testId,
}: {
  rows: readonly WizardReviewRow[]
  onEdit: (stepId: string) => void
  testId: string
}): JSX.Element {
  return (
    <Box data-testid={testId} sx={{ display: 'grid' }}>
      {groupByStep(rows).map(group => {
        const stepId = group[0].stepId
        return (
          <Box
            key={stepId}
            data-testid={`${testId}-${stepId}`}
            sx={{
              display: 'grid',
              gridTemplateColumns: 'minmax(0, 1fr) auto',
              columnGap: 2,
              alignItems: 'start',
              py: 1.5,
              borderBottom: 1,
              borderColor: 'divider',
              '&:first-of-type': { pt: 0 },
            }}
          >
            <KeyValueList labelWidth="9rem">
              {/* Keyed by label: rows come and go, and an index key would hand one row's state to another. */}
              {group.map(row => <KeyValue key={row.label} label={row.label}>{row.value}</KeyValue>)}
            </KeyValueList>
            <Link
              component="button"
              type="button"
              variant="body2"
              onClick={() => onEdit(stepId)}
              aria-label={`Edit ${group.map(r => r.label).join(', ')}`}
              data-testid={`${testId}-edit-${stepId}`}
            >
              Edit
            </Link>
          </Box>
        )
      })}
    </Box>
  )
}

export default WizardReview
