/* HOW THE CRON CARD WILL SPELL AN EXPRESSION once it has parsed it — `material-ui-cron`'s own grammar
 * (`src/selector.ts::cronExpState`: written, it parses the expression into the field atoms; read, it composes
 * them back), run in a private store, so `ScheduleEditor.tsx` knows in advance whether the card's parse will
 * emit a re-spelling. The card's own atoms, never a copy of its rules. It reads the package's `src` beside the
 * `dist` the card runs, which agree only because `material-ui-cron` is pinned exactly (and `jotai` with it): a bump
 * of either re-runs `e2e/cron-schedule-editor.spec.ts`, whose re-spelled case hangs the card hidden if they part. */
import { createStore } from 'jotai'
import { cronExpState } from 'material-ui-cron/src/selector'
import { cronValidationErrorMessageState } from 'material-ui-cron/src/store'

/** The card's spelling of `expression`; `expression` itself when the card refuses it (it parses nothing then). */
export function cardSpelling(expression: string): string {
  const store = createStore()
  store.set(cronExpState, expression)
  return store.get(cronValidationErrorMessageState) ? expression : store.get(cronExpState)
}
