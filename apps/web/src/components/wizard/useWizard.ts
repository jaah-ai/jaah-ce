import { useEffect, useState } from 'react'

/* THE WIZARD — a stepper for long CREATE forms and multi-step purchase flows (the change-plan wizard,
 * `pages/org/billing/ChangePlanWizard.tsx`), never an edit page, never a tab strip (tabs are `components/Tabs.tsx`). Four pieces, one per file in this directory:
 *   - `useWizard(steps)` (here): which step is showing, how far the user got, Next/Back/goTo, the block.
 *   - `WizardSteps`: the indicator — a full-label MUI `Stepper` at >= 600px, "Step 2 of 5 · When" plus a
 *     thin progress bar below it (never a row of truncated labels).
 *   - `WizardStepPanel`: wrap EACH step's fields in one. Every panel stays MOUNTED and only the current one
 *     is shown (`hidden`), like `TabPanel` — so a value typed on step 1 survives Next → Back, and the
 *     consumer keeps ONE form state for the whole create.
 *   - `WizardFooter`: Back / Next / Create on `ui/FormActionBar`, plus the blocked message. Create runs
 *     `validateAll()` first; nothing is posted before Create — the consumer's `onCreate` is the only POST.
 *   - `WizardReview`: the last step's read-only summary, one Edit link per step (→ `goTo`).
 * Step state is component state, NOT the URL (no `?step=`): a reload starts the create over, which is
 * what an unsaved create should do.
 *
 * Consumer shape:
 *   const steps = [{ id: 'what', label: 'What', valid: name !== '', invalidField: { inputId: 'x-name', label: 'Name' } }, …]
 *   const wizard = useWizard(steps)
 *   <WizardSteps steps={steps} wizard={wizard} label="New X steps" testId="x-steps" />
 *   <WizardStepPanel id="what" wizard={wizard}>…fields…</WizardStepPanel>
 *   <WizardFooter wizard={wizard} onCreate={save} creating={busy} testId="x-actions" />
 *
 * Steps may come and go (a step omitted by a predicate): the hook tracks the current step by ID and falls
 * back to the same POSITION when that id leaves the list; "reached" is the set of step ids shown so far, and
 * `furthest` is the last step of the unbroken reached run from step 1 — so a step a predicate INSERTS is
 * never counted as reached, `index <= furthest` always holds, and both stay inside the list. The list must
 * hold at least one step.
 *
 * `initial` resumes a position (`reached` exposes it to save): the consumer carrying its draft across a remount
 * (`hooks/useRemountDraft.ts`) carries where the user was too. */

export type WizardStep<Id extends string> = {
  id: Id
  label: string
  /** False blocks Next (and Create) on this step. */
  valid: boolean
  /** The field Next focuses and names when `valid` is false. */
  invalidField?: { inputId: string; label: string }
  /** Overrides the default "<field> is required" — e.g. "Pick at least one day". */
  invalidMessage?: string
}

/** What a step says when Next is refused on it. */
export function blockedMessageFor(step: WizardStep<string>): string {
  if (step.invalidMessage) return step.invalidMessage
  if (step.invalidField) return `${step.invalidField.label} is required`
  return `Complete ${step.label} first`
}

type Mark<Id> = { id: Id; index: number }

/** The mark's index in `steps`: by id while that id is listed, else its last position clamped into the list. */
function resolve<Id extends string>(mark: Mark<Id>, steps: readonly WizardStep<Id>[]): number {
  const found = steps.findIndex(s => s.id === mark.id)
  return found >= 0 ? found : Math.max(0, Math.min(mark.index, steps.length - 1))
}

/** The last index of the unbroken run of reached steps from the first (-1 when step 1 is not reached). */
function reachedRun<Id extends string>(reached: ReadonlySet<Id>, steps: readonly WizardStep<Id>[]): number {
  const gap = steps.findIndex(s => !reached.has(s.id))
  return gap < 0 ? steps.length - 1 : gap - 1
}

export function useWizard<Id extends string>(
  steps: readonly WizardStep<Id>[],
  initial?: { step: Id; index: number; reached: readonly Id[] } | null,
) {
  if (steps.length === 0) throw new Error('useWizard needs at least one step')
  // A resumed step not listed yet keeps its position, as `resolve` does.
  const [current, setCurrent] = useState<Mark<Id>>(() => (initial ? { id: initial.step, index: initial.index } : { id: steps[0].id, index: 0 }))
  const [reached, setReached] = useState<ReadonlySet<Id>>(() => new Set([steps[0].id, ...(initial?.reached ?? [])]))
  // The step whose Next was refused; the message shows only while THAT step is current and still invalid.
  const [blockedOn, setBlockedOn] = useState<Id | null>(null)
  // A focus request, applied after the render that shows its panel (a hidden input cannot take focus).
  const [focusRequest, setFocusRequest] = useState<{ inputId: string; n: number } | null>(null)

  const index = resolve(current, steps)
  const furthest = Math.max(index, reachedRun(reached, steps))
  const step = steps[index]

  useEffect(() => {
    if (focusRequest) document.getElementById(focusRequest.inputId)?.focus()
  }, [focusRequest])

  const show = (i: number) => {
    const target = steps[i]
    setCurrent({ id: target.id, index: i })
    setReached(r => (r.has(target.id) ? r : new Set(r).add(target.id)))
    setBlockedOn(null)
  }

  const block = (s: WizardStep<Id>) => {
    setBlockedOn(s.id)
    if (s.invalidField) setFocusRequest(r => ({ inputId: s.invalidField!.inputId, n: (r?.n ?? 0) + 1 }))
  }

  return {
    step: step.id,
    index,
    isFirst: index === 0,
    isLast: index === steps.length - 1,
    furthest,
    reached: [...reached],
    blockedMessage: blockedOn === step.id && !step.valid ? blockedMessageFor(step) : null,
    next: () => {
      if (!step.valid) return block(step)
      if (index < steps.length - 1) show(index + 1)
    },
    back: () => {
      if (index > 0) show(index - 1)
    },
    /** Only to a step already reached (`<= furthest`); anything else is refused (a no-op). */
    goTo: (id: Id) => {
      const i = steps.findIndex(s => s.id === id)
      if (i >= 0 && i <= furthest) show(i)
    },
    /** Create's guard: true when every step is valid; otherwise shows the first invalid step, blocked. */
    validateAll: (): boolean => {
      const i = steps.findIndex(s => !s.valid)
      if (i < 0) return true
      setCurrent({ id: steps[i].id, index: i })
      block(steps[i])
      return false
    },
  }
}

export type Wizard<Id extends string> = ReturnType<typeof useWizard<Id>>
