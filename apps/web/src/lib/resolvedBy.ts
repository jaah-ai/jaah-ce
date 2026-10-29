/* The Q&A BYLINE — who settled a question, and when, as one line. Shared by both Q&A surfaces
 * (`components/tasks/GateForm.tsx`'s round history and `pages/taskDetail/TaskActivityPanel.tsx`'s `QATrail`) so they
 * cannot drift. Pure of React (like `authorBadge.ts`, `relativeTime.ts`): this owns what the line SAYS.
 * Relative here, absolute on hover — callers must put `absoluteTime()` on the element's `title`.
 */
import { NO_VALUE, relativeAge } from './relativeTime'

/**
 * The byline verb from the round's `status`. `answered` and `dismissed` must never be conflated (a
 * dismissal records no answer); any unknown status degrades to the neutral `Resolved` (as `GateForm`'s
 * status chip).
 */
export function resolvedVerb(status: string): string {
  if (status === 'answered') return 'Answered'
  if (status === 'dismissed') return 'Dismissed'
  return 'Resolved'
}

/**
 * What a dismissed round says where an answer would go — one spelling for both surfaces. Never fabricates
 * an answer, and claims only the round's own outcome: not that the runner continued (dismissing round 3 of
 * 8 leaves it parked on the other seven).
 */
export const DISMISSED_OUTCOME_TEXT = 'Ignored — dismissed without an answer.'

/** What a byline needs: who settled the round, when, and which outcome it was. */
export type ResolvedByInput = {
  by: string | null | undefined
  at: string | null | undefined
  status: string
}

/**
 * The byline text — `Answered by Ada Lovelace · 3d ago` — or `null`. Each half degrades on its own:
 *   · no name, a time → `Answered · 3d ago` (pre-migration-0135 rows, deleted accounts);
 *   · a name, no time → `Answered by Ada Lovelace` (not written today; old rows are read forever);
 *   · neither → `null`: never "Answered by —", which asserts an untraceable answer.
 * An unparseable timestamp counts as absent (no em-dash inside the sentence).
 */
export function resolvedByLine(input: ResolvedByInput, now: Date = new Date()): string | null {
  const verb = resolvedVerb(input.status)
  const name = typeof input.by === 'string' && input.by.trim() !== '' ? input.by.trim() : null
  const rawAge = input.at ? relativeAge(input.at, now) : NO_VALUE
  const age = rawAge === NO_VALUE ? null : rawAge
  if (name && age) return `${verb} by ${name} · ${age}`
  if (name) return `${verb} by ${name}`
  if (age) return `${verb} · ${age}`
  return null
}
