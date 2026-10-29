import { bootEstimateS } from '../../lib/bootEstimate'
import type { NodeOs } from '../../lib/nodeOs'
import type { OrderStage } from './api'

/* AN ORDER'S COUNTDOWN — a fixed guess per stage the system works through, counted from when the order
 * entered its stage (`updated_at`), so a reopened dialog or a retried stage counts from the right moment.
 *
 * Only the system's own stages are timed: `paying` and `waiting_for_machine` wait on the person, so no countdown
 * runs while either is current — until a self-hosted machine fetches its install script: from then
 * `waiting_for_machine` is the install, timed from that fetch. */

/** Seconds each system stage usually takes, but `launching`: that is the machine's boot (`bootEstimateS`), which its
 *  OS and instance type decide; a Windows order (no `joining`) boots inside it. */
export const STAGE_ESTIMATE_S: Partial<Record<Exclude<OrderStage, 'launching'>, number>> = {
  joining: 60,
  placing: 60,
}

/** Seconds a self-hosted install usually takes, from its script's fetch until the machine reports in: it upgrades
 *  the OS packages first. */
export const INSTALL_ESTIMATE_S = 300

/** The launched machine, when the order names it: its boot sets the `launching` estimate; a self-hosted one's
 *  install fetch (`install_fetched_at`) times `waiting_for_machine`. */
export type OrderMachine = { os?: NodeOs | null; instanceType?: string | null; installFetchedAt?: string | null }

/** Past this many times its estimate, the current stage is "taking longer than usual". */
const SLOW_FACTOR = 2
/** The bar never reads full before the node is ready. */
const BAR_CAP = 95

export type OrderEta = {
  /** Seconds left by the estimate; 0 once it has run out. */
  remainingS: number
  /** Bar fill, 0–`BAR_CAP`. */
  percent: number
  /** The current stage passed its estimate: the bar holds. */
  overrun: boolean
  /** The estimate ran out with no timed stage left: "Almost done…". */
  almostDone: boolean
  /** The current stage passed `SLOW_FACTOR` times its estimate. */
  slow: boolean
}

/** The countdown for an order in `stage`, which it entered at `since`; null when `stage` is not a timed one. */
export function orderEta(
  stages: readonly OrderStage[], stage: OrderStage, since: string | null | undefined, now: number, machine: OrderMachine = {},
): OrderEta | null {
  const stageEstimate = (s: OrderStage) => (s === 'launching' ? bootEstimateS(machine.os, machine.instanceType)
    : s === 'waiting_for_machine' ? (machine.installFetchedAt ? INSTALL_ESTIMATE_S : undefined)
    : STAGE_ESTIMATE_S[s])
  const own = stageEstimate(stage)
  // The install runs from its fetch, not from when the order began waiting.
  const from = stage === 'waiting_for_machine' ? machine.installFetchedAt : since
  const at = from ? Date.parse(from) : NaN
  if (own === undefined || Number.isNaN(at)) return null
  const index = stages.indexOf(stage)
  if (index < 0) return null
  const estimate = (list: readonly OrderStage[]) => list.reduce((sum, s) => sum + (stageEstimate(s) ?? 0), 0)
  const before = estimate(stages.slice(0, index))
  const after = estimate(stages.slice(index + 1))
  // A client clock behind the server's would read a negative age.
  const elapsed = Math.max(0, (now - at) / 1000)
  const inStage = Math.min(elapsed, own)
  const remainingS = Math.ceil(own - inStage + after)
  return {
    remainingS,
    percent: Math.min(BAR_CAP, ((before + inStage) / (before + own + after)) * 100),
    overrun: elapsed >= own,
    almostDone: elapsed >= own && after === 0,
    slow: elapsed > own * SLOW_FACTOR,
  }
}

/** `3:05` for 185 seconds. */
export function formatRemaining(seconds: number): string {
  const s = Math.max(0, Math.ceil(seconds))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}
