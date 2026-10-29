import type { JSX, ReactNode } from 'react'
import type { Wizard } from './useWizard'

/* One step's fields. ALWAYS MOUNTED, shown only while its step is current (`hidden`, as `TabPanel` does), so
 * the inputs keep their values and the hook can focus the invalid one (module contract: `useWizard.ts`). */
export function WizardStepPanel<Id extends string>({
  id,
  wizard,
  label,
  children,
}: {
  id: Id
  wizard: Wizard<Id>
  /** The panel's accessible name — usually the step's label. */
  label?: string
  children: ReactNode
}): JSX.Element {
  return (
    <div role="group" aria-label={label} data-wizard-step={id} hidden={wizard.step !== id}>
      {children}
    </div>
  )
}

export default WizardStepPanel
