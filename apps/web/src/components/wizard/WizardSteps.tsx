import type { JSX } from 'react'
import Box from '@mui/material/Box'
import LinearProgress from '@mui/material/LinearProgress'
import Step from '@mui/material/Step'
import StepButton from '@mui/material/StepButton'
import Stepper from '@mui/material/Stepper'
import Typography from '@mui/material/Typography'
import useMediaQuery from '@mui/material/useMediaQuery'
import type { Wizard, WizardStep } from './useWizard'

/* The wizard's step indicator (module contract: `useWizard.ts`'s header). Two layouts, never a third:
 *   - >= 600px (MUI `sm`): one compact horizontal `Stepper` line, each label in full beside its icon (never
 *     ellipsized); a step already reached is a button back to it, a future step is disabled.
 *   - < 600px: ONE line, "Step 2 of 5 · When", over a thin progress bar. A row of five labels cannot stay
 *     legible at 390px, so the phone gets the current step's name in full rather than all five shredded. */
export const WIZARD_NARROW_QUERY = '(max-width:599.95px)'

export function WizardSteps<Id extends string>({
  steps,
  wizard,
  label,
  testId,
}: {
  steps: readonly WizardStep<Id>[]
  wizard: Wizard<Id>
  /** The indicator's accessible name ("New automation steps"). */
  label: string
  testId: string
}): JSX.Element {
  const narrow = useMediaQuery(WIZARD_NARROW_QUERY, { noSsr: true })
  const { index, furthest } = wizard

  if (narrow) {
    const position = `Step ${index + 1} of ${steps.length}`
    return (
      <Box component="nav" aria-label={label} data-testid={testId} sx={{ display: 'grid', gap: 0.75 }}>
        <Typography variant="body2" data-testid={`${testId}-count`} sx={{ fontWeight: 500 }}>
          {position} · <Box component="span" aria-current="step">{steps[index].label}</Box>
        </Typography>
        <LinearProgress
          variant="determinate"
          value={((index + 1) / steps.length) * 100}
          aria-label={position}
          sx={{ height: 4, borderRadius: 2 }}
        />
      </Box>
    )
  }

  return (
    <Box component="nav" aria-label={label} data-testid={testId}>
      <Stepper nonLinear activeStep={index}>
        {steps.map((s, i) => (
          // MUI renders a non-linear stepper as a tablist (each StepButton a `tab`, roving focus): the
          // current step is the selected tab, a future step a disabled one.
          <Step key={s.id} completed={i < furthest && i !== index && s.valid} disabled={i > furthest}>
            <StepButton onClick={() => wizard.goTo(s.id)}>{s.label}</StepButton>
          </Step>
        ))}
      </Stepper>
    </Box>
  )
}

export default WizardSteps
