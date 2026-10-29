import type { SetupChecklistStep } from '../setupChecklist/SetupChecklist'

/* WHAT A GUIDE HANDS THE FLOATING WIDGET (`OnboardingWidget.tsx`). Each guide hook — the org's "Set up your
 * company" (`companySetupGuide.tsx`) and the user's "Get started" (`getStartedGuide.ts`) — owns its own
 * steps, completion and dismissal and answers with one of these; the widget owns the ONE frame both are drawn
 * in and the rule that picks which (never both). Splitting it this way keeps every per-guide rule in the
 * guide's file and the choice in exactly one place. */

/** One guide, ready to draw. */
export type Guide = {
  /** The header's title. */
  title: string
  /** Every test id is `<prefix>-…` (`SetupChecklist.tsx` lists them). Kept per guide so specs can tell the
   *  two apart: `company-setup-…` and `get-started-…`. */
  testIdPrefix: string
  /** The ✕'s accessible name; the glyph is `aria-hidden`. The ✕ HIDES: the sidebar's Setup button shows the
   *  guide again until every step is done. */
  dismissLabel: string
  steps: SetupChecklistStep[]
  /** Mark the one step to do next (the company guide). */
  highlightNext: boolean
  /** On a desktop with no stored choice, start OPEN even on a narrow viewport (the company guide: a new org's
   *  first screen is this list, and its board is empty). Absent, a narrow viewport starts on the rail. */
  startOpen?: boolean
  /** Each step's label is its link and no trailing destination link is drawn (the Get started card). */
  labelLinks?: boolean
  /** The user hid it (stored per user): the widget draws nothing, the sidebar's Setup button still counts it. */
  hidden: boolean
  /** Stores the per-user hide; resolves to whether the write landed. */
  onDismiss: () => Promise<boolean>
  /** Clears it (the sidebar's Setup button); resolves to whether the write landed. */
  onShow: () => Promise<boolean>
}

/** A guide hook's answer. `pending`: it cannot answer yet, so the widget draws nothing (no flash of the
 *  wrong guide). Otherwise `guide` is what to draw, or null when there is nothing to draw. */
export type GuideAnswer = { pending: boolean; guide: Guide | null }
