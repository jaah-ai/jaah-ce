import type { TaskQuestion } from '../types'

/* Did the agent pick up the answers handed to it? The CP records only the hand-over (`TaskQuestion.delivered_at`);
 * whether the agent's own request was decided lives on the node. The agent's event stream is the one witness the
 * console can read: any event after the hand-over means the turn resumed. None for a minute reads as "not picked
 * up" — a task stuck Running with nothing happening. A host that keeps receipts also says
 * whether the answer reached it at all, which tells a lost hand-over from an agent that took it and stalled. */

/** How long an answer may sit with no agent event before the task page says so. */
export const PICKUP_GRACE_MS = 60_000

/** Event stamps come from the node's clock, the hand-over from the CP's: an event this close before it still counts. */
export const CLOCK_SKEW_MS = 5_000

/** The newest hand-over among settled rounds, or null when nothing was handed over or a round is open again
 *  (an open round is the agent asking anew, so an earlier answer is not the thing it waits on). `since`: the
 *  live session's start — a hand-over before it went to an earlier run, which a retry left behind. */
export function lastHandOver(questions: TaskQuestion[], since?: string): string | null {
  if (questions.some(q => q.status === 'open')) return null
  const floor = since ? Date.parse(since) : -Infinity
  let newest: string | null = null
  for (const q of questions) {
    const at = q.delivered_at ? Date.parse(q.delivered_at) : NaN
    if (at >= floor && (newest === null || at > Date.parse(newest))) newest = q.delivered_at as string
  }
  return newest
}

/** The node host's receipt for an answer (`apps/jaahd/acp/events.py::answer_received`): the host's own
 *  record, never the agent resuming. */
export const RECEIPT_KIND = 'answer_received'

/** The slice of an `AgentEvent` read here; `kind`/`data` absent reads as an agent event with no receipt. */
export type PickupEvent = { ts: string; kind?: string; data?: Record<string, unknown> | null }

/** What the node said about the answer: `received` (fed to the agent, or queued as its next prompt), `dropped`,
 *  `none` (no receipt from a host that keeps them: the answer never reached it), or `null` — the host predates
 *  receipts (`session_start.data.answer_receipts`), so no claim either way. */
export type AnswerReceipt = 'received' | 'dropped' | 'none' | null

/** An answer the agent never picked up: when it was handed over, how long ago, and the node's receipt. */
export type StalledAnswer = { handedAt: string; silentMs: number; receipt: AnswerReceipt }

/** True when some AGENT event is stamped after `handedAt` (less the skew allowance); a receipt is not one. */
export function pickedUp(handedAt: string, events: PickupEvent[]): boolean {
  const at = Date.parse(handedAt) - CLOCK_SKEW_MS
  return events.some(e => e.kind !== RECEIPT_KIND && Date.parse(e.ts) > at)
}

/** The node's newest receipt after `handedAt` (same skew allowance), read as `AnswerReceipt`. */
export function answerReceipt(handedAt: string, events: PickupEvent[]): AnswerReceipt {
  if (!events.some(e => e.kind === 'session_start' && e.data?.answer_receipts === true)) return null
  const at = Date.parse(handedAt) - CLOCK_SKEW_MS
  const last = events.filter(e => e.kind === RECEIPT_KIND && Date.parse(e.ts) > at).at(-1)
  if (!last) return 'none'
  return last.data?.decision === 'dropped' ? 'dropped' : 'received'
}

/** The answer still unheard past the grace window: when it was handed over, how long ago, and what the node said
 *  of it. Null otherwise. */
export function stalledAnswer(
  questions: TaskQuestion[],
  events: PickupEvent[],
  now: number,
  since?: string,
): StalledAnswer | null {
  const handedAt = lastHandOver(questions, since)
  if (handedAt === null || pickedUp(handedAt, events)) return null
  const silentMs = now - Date.parse(handedAt)
  return silentMs >= PICKUP_GRACE_MS ? { handedAt, silentMs, receipt: answerReceipt(handedAt, events) } : null
}
