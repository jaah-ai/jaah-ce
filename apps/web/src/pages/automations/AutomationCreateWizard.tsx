import { useEffect, type ReactNode } from 'react'
import Box from '@mui/material/Box'
import { useWizard, type WizardStep } from '../../components/wizard/useWizard'
import WizardFooter from '../../components/wizard/WizardFooter'
import WizardStepPanel from '../../components/wizard/WizardStepPanel'
import WizardSteps from '../../components/wizard/WizardSteps'
import Banner from '../../ui/Banner'
import { DetailPage } from '../../ui/detail/DetailPage'
import { grid } from '../../theme'

/* NEW AUTOMATION, THREE STEPS (`/automations/new`): When → What → Name, Create on Name. Only the essentials are asked
 * (the same `AutomationSections.tsx` parts the edit page's Setup tab draws); every other setting keeps its default
 * and is changed later on the edit page's Settings tab. This is only the wizard's frame (`components/wizard/`): the
 * draft, each step's blocker and the one `save()` stay `AutomationEditPage.tsx`'s. Every step's panel stays mounted,
 * so nothing typed is lost going Back, and nothing is posted until Create. */

export type AutomationStepId = 'when' | 'what' | 'name'

/** What stops a step's Next — the editor's own sentence, which names the field — or null. */
export type StepBlockers = Record<AutomationStepId, string | null>

/** Where the user is in the wizard (`useWizard`'s `initial`), carried across a remount by the page. */
export type WizardPosition = { step: AutomationStepId; index: number; reached: AutomationStepId[] }

const LABELS: Record<AutomationStepId, string> = { when: 'When', what: 'What', name: 'Name' }
/** The field Create focuses when Name is refused. */
const NAME_INPUT_ID = 'automation-name'

export default function AutomationCreateWizard({
  breadcrumb, blockers, when, what, name, onCreate, onCancel, creating, saveError, errorStep, onBlocked, resume, onPosition,
  children,
}: {
  breadcrumb: ReactNode
  blockers: StepBlockers
  when: ReactNode
  what: ReactNode
  name: ReactNode
  onCreate: () => void
  onCancel: () => void
  creating: boolean
  saveError: string
  /** The step a refused Create belongs on (a params refusal → What); the wizard opens it. */
  errorStep: AutomationStepId | null
  /** Told the step whose Next is refused while its message shows (null once it clears), so a step can say it in place. */
  onBlocked?: (step: AutomationStepId | null) => void
  /** The position to open at (a remount's carry-over), or null for When. */
  resume?: WizardPosition | null
  /** Told the position after every render, so the page can carry it. */
  onPosition?: (position: WizardPosition) => void
  /** Dialogs (secret, unsaved changes). */
  children?: ReactNode
}) {
  const steps: WizardStep<AutomationStepId>[] = (['when', 'what', 'name'] as const).map(id => ({
    id, label: LABELS[id], valid: blockers[id] === null, invalidMessage: blockers[id] ?? undefined,
    ...(id === 'name' ? { invalidField: { inputId: NAME_INPUT_ID, label: 'Name' } } : null),
  }))
  const wizard = useWizard(steps, resume)
  useEffect(() => {
    onPosition?.({ step: wizard.step, index: wizard.index, reached: wizard.reached })
  })

  const blockedStep = wizard.blockedMessage === null ? null : wizard.step
  useEffect(() => {
    onBlocked?.(blockedStep)
    // Only a change of the refused step is news; the callback is re-minted every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [blockedStep])

  const { goTo } = wizard
  useEffect(() => {
    if (errorStep !== null) goTo(errorStep)
    // Only a new refusal moves the wizard; `goTo` is re-minted every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [errorStep, saveError])

  return (
    <DetailPage
      breadcrumb={breadcrumb}
      title="New automation"
      headerTestId="automation-header"
      actionBar={(
        <Box sx={{ pt: 2, maxWidth: grid(128) }}>
          {saveError && <Banner severity="error" testId="automation-save-error">{saveError}</Banner>}
          <WizardFooter wizard={wizard} onCreate={onCreate} onCancel={onCancel} creating={creating} testId="automation-actions" />
        </Box>
      )}
    >
      <Box sx={{ mb: 3, maxWidth: grid(128) }}>
        <WizardSteps steps={steps} wizard={wizard} label="New automation steps" testId="automation-steps" />
      </Box>
      <WizardStepPanel id="when" wizard={wizard} label="When">{when}</WizardStepPanel>
      <WizardStepPanel id="what" wizard={wizard} label="What">{what}</WizardStepPanel>
      <WizardStepPanel id="name" wizard={wizard} label="Name">{name}</WizardStepPanel>
      {children}
    </DetailPage>
  )
}
