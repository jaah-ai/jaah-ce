import { useEffect, type ReactNode } from 'react'
import Box from '@mui/material/Box'
import { useWizard, type WizardStep } from '../../components/wizard/useWizard'
import WizardFooter from '../../components/wizard/WizardFooter'
import WizardReview, { type WizardReviewRow } from '../../components/wizard/WizardReview'
import WizardStepPanel from '../../components/wizard/WizardStepPanel'
import WizardSteps from '../../components/wizard/WizardSteps'
import Banner from '../../ui/Banner'
import { DetailPage, DetailSection } from '../../ui/detail/DetailPage'
import type { AutomationStepId } from './AutomationReview'

/* NEW AUTOMATION, ONE STEP AT A TIME (`/automations/new`): About → When → Target → Action → Review, Create on
 * Review. Target is omitted exactly when the editor would not show it (`takesTarget`). This is only the
 * wizard's frame (`components/wizard/`): the parts are the SAME `AutomationSections.tsx` the edit page's Setup
 * tab draws, and the draft, each step's blocker and the one `save()` stay `AutomationEditPage.tsx`'s — so a
 * field exists once. Every step's panel stays mounted, so nothing typed is lost going Back, and nothing is
 * posted until Create. */

/** What stops a step's Next — the editor's own sentence, which names the field — or null. */
export type StepBlockers = { about: string | null; when: string | null; target: string | null; action: string | null }

/** Where the user is in the wizard (`useWizard`'s `initial`), carried across a remount by the page. */
export type WizardPosition = { step: AutomationStepId; index: number; reached: AutomationStepId[] }

const LABELS: Record<AutomationStepId, string> = { about: 'About', when: 'When', target: 'Target', action: 'Action', review: 'Review' }
/** The field Next focuses when About is refused (the only step whose blocker is one plain input). */
const NAME_INPUT_ID = 'automation-name'

export default function AutomationCreateWizard({
  breadcrumb, blockers, nameMissing, showsTarget, about, when, target, action, reviewRows, onCreate, onCancel, creating,
  saveError, errorStep, resume, onPosition, children,
}: {
  breadcrumb: ReactNode
  blockers: StepBlockers
  /** About's blocker is the empty Name (Next focuses it), not the org. */
  nameMissing: boolean
  showsTarget: boolean
  about: ReactNode
  when: ReactNode
  target: ReactNode
  action: ReactNode
  reviewRows: readonly WizardReviewRow[]
  onCreate: () => void
  onCancel: () => void
  creating: boolean
  saveError: string
  /** The step a refused Create belongs on (a params refusal → Action); the wizard opens it. */
  errorStep: AutomationStepId | null
  /** The position to open at (a remount's carry-over), or null for About. */
  resume?: WizardPosition | null
  /** Told the position after every render, so the page can carry it. */
  onPosition?: (position: WizardPosition) => void
  /** Dialogs (secret, unsaved changes). */
  children?: ReactNode
}) {
  const step = (id: AutomationStepId, blocker: string | null): WizardStep<AutomationStepId> => ({
    id, label: LABELS[id], valid: blocker === null, invalidMessage: blocker ?? undefined,
    ...(id === 'about' && nameMissing ? { invalidField: { inputId: NAME_INPUT_ID, label: 'Name' } } : null),
  })
  const steps: WizardStep<AutomationStepId>[] = [
    step('about', blockers.about),
    step('when', blockers.when),
    ...(showsTarget ? [step('target', blockers.target)] : []),
    step('action', blockers.action),
    step('review', null),
  ]
  const wizard = useWizard(steps, resume)
  useEffect(() => {
    onPosition?.({ step: wizard.step, index: wizard.index, reached: wizard.reached })
  })

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
        <Box sx={{ pt: 2, maxWidth: '48rem' }}>
          {saveError && <Banner severity="error" testId="automation-save-error">{saveError}</Banner>}
          <WizardFooter wizard={wizard} onCreate={onCreate} onCancel={onCancel} creating={creating} testId="automation-actions" />
        </Box>
      )}
    >
      <Box sx={{ mb: 3, maxWidth: '48rem' }}>
        <WizardSteps steps={steps} wizard={wizard} label="New automation steps" testId="automation-steps" />
      </Box>
      <WizardStepPanel id="about" wizard={wizard} label="About">{about}</WizardStepPanel>
      <WizardStepPanel id="when" wizard={wizard} label="When">{when}</WizardStepPanel>
      {showsTarget && <WizardStepPanel id="target" wizard={wizard} label="Target">{target}</WizardStepPanel>}
      <WizardStepPanel id="action" wizard={wizard} label="Action">{action}</WizardStepPanel>
      <WizardStepPanel id="review" wizard={wizard} label="Review">
        <DetailSection title="Review" testId="automation-section-review">
          <Box sx={{ maxWidth: '48rem' }}>
            {/* Drawn only while shown: its rows read names from the server, which a hidden step should not ask for
                on every keystroke of an earlier one. */}
            {wizard.step === 'review' && (
              <WizardReview rows={reviewRows} onEdit={id => wizard.goTo(id as AutomationStepId)} testId="automation-review" />
            )}
          </Box>
        </DetailSection>
      </WizardStepPanel>
      {children}
    </DetailPage>
  )
}
