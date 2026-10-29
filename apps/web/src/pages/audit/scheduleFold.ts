import type { FleetCronRun } from './scheduleApi'

/* THE SCHEDULE LEDGER'S FOLD — a stretch of consecutive `missed` firings is one line. A fresh stack, or
 * a control plane that was down, reconstructs one missed firing per cron, all at once: each says the same word
 * ("Missed."), so a page of them buried the firings that did something. Across crons, unlike Automations › Runs'
 * per-automation `groupRuns`: here the neighbours are different crons missed in the same gap. Consecutive only, so
 * the log keeps its order; a stretch of one stays an ordinary row. */

export type ScheduleLine =
  | { kind: 'run'; run: FleetCronRun }
  | {
      kind: 'missed'
      /** Keyed on the OLDEST firing: a newer missed firing joins at the top, so an open fold stays open. */
      key: string
      /** Newest first, as served; two or more. */
      runs: FleetCronRun[]
    }

const MISSED = 'missed'

export function foldMissed(runs: FleetCronRun[]): ScheduleLine[] {
  const lines: ScheduleLine[] = []
  let stretch: FleetCronRun[] = []

  function flush() {
    if (stretch.length === 1) lines.push({ kind: 'run', run: stretch[0] })
    else if (stretch.length > 1) lines.push({ kind: 'missed', key: `missed-${stretch[stretch.length - 1].id}`, runs: stretch })
    stretch = []
  }

  for (const run of runs) {
    if (run.outcome === MISSED) {
      stretch.push(run)
      continue
    }
    flush()
    lines.push({ kind: 'run', run })
  }
  flush()
  return lines
}

/** Cron names spelled out in a folded line's title before "+N more". */
export const FOLD_MAX_NAMES = 3

/** A folded line's sentence: `Runner dispatch — Missed ×4.` for one cron, else `Missed ×6 — Runner dispatch,
 *  Retention sweep, +2 more.` — distinct crons, newest first. */
export function missedSentence(runs: FleetCronRun[]): string {
  const names = [...new Set(runs.map(r => r.cron_name))]
  if (names.length === 1) return `${names[0]} — Missed ×${runs.length}.`
  const shown = names.slice(0, FOLD_MAX_NAMES)
  const rest = names.length - shown.length
  return `Missed ×${runs.length} — ${shown.join(', ')}${rest > 0 ? `, +${rest} more` : ''}.`
}
