import type { JSX } from 'react'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import FormActionBar from '../../ui/FormActionBar'
import { tapTargetSx } from '../../ui/listStyles'
import type { Wizard } from './useWizard'

/* The wizard's action row on `ui/FormActionBar` (its `plain` row, under the step) (module contract: `useWizard.ts`): optional Cancel, Back (absent on step 1),
 * Next on every step but the last, Create on the last. Every button is `type="button"`, so Enter in a field
 * submits nothing — `onCreate` is the only way out of the wizard to the server, and it runs only once
 * `validateAll()` passes. The blocked message sits at the row's start, announced as an alert. */
export function WizardFooter<Id extends string>({
  wizard,
  onCreate,
  creating = false,
  createLabel = 'Create',
  busyLabel = 'Creating…',
  disabled = false,
  nextDisabled = false,
  onNext,
  onCancel,
  testId,
}: {
  wizard: Wizard<Id>
  /** The POST. Set `creating` while it runs: that is what disables Create against a second click. */
  onCreate: () => void
  creating?: boolean
  createLabel?: string
  /** Create's label while `creating`. */
  busyLabel?: string
  /** Disables Create without the busy label (a price still loading). */
  disabled?: boolean
  /** Disables Next: the step has no way forward from here (its own words say why and where to go instead). */
  nextDisabled?: boolean
  /** Replaces Next's plain advance, for a step that saves first; it calls `wizard.next()` itself. */
  onNext?: () => void
  /** Leaves the create (the consumer's unsaved-changes gate); a text button before Back. Absent: no Cancel. */
  onCancel?: () => void
  testId: string
}): JSX.Element {
  const primary = wizard.isLast ? `${testId}-create` : `${testId}-next`
  return (
    <FormActionBar testId={testId} stretchTestIds={[primary]} plain>
      {wizard.blockedMessage && (
        <Typography
          role="alert"
          variant="body2"
          data-testid={`${testId}-blocked`}
          sx={{ color: 'error.main', alignSelf: 'center', mr: 'auto' }}
        >
          {wizard.blockedMessage}
        </Typography>
      )}
      {onCancel && (
        <Button type="button" variant="text" color="inherit" onClick={onCancel} data-testid={`${testId}-cancel`} sx={tapTargetSx}>
          Cancel
        </Button>
      )}
      {!wizard.isFirst && (
        <Button type="button" variant="text" color="inherit" onClick={wizard.back} data-testid={`${testId}-back`} sx={tapTargetSx}>
          Back
        </Button>
      )}
      {wizard.isLast ? (
        <Button
          type="button"
          variant="contained"
          disabled={creating || disabled}
          onClick={() => { if (wizard.validateAll()) onCreate() }}
          data-testid={primary}
          sx={tapTargetSx}
        >
          {creating ? busyLabel : createLabel}
        </Button>
      ) : (
        <Button type="button" variant="contained" disabled={nextDisabled} onClick={onNext ?? wizard.next} data-testid={primary} sx={tapTargetSx}>
          Next
        </Button>
      )}
    </FormActionBar>
  )
}

export default WizardFooter
