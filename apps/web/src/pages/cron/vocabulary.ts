/* THE CRON SURFACE'S PRESENTATION VOCABULARY — how the server's machine tokens READ to a person, shared
 * by the Automations surface and Audit's Schedule ledger so no two screens word or colour a token differently.
 * EVERY LOOKUP HAS A FALLBACK: the sets are closed server-side (`app/crons.py::RUN_OUTCOMES`,
 * `SKIP_REASONS`, `RETENTION_MODES`) but not on the wire, so an unknown
 * token renders AS ITSELF (see `pages/cron/api.ts`).
 */

import type { CronRun } from './api'
import { NO_VALUE } from '../../lib/relativeTime'
import { formatSpanBetween } from '../../lib/spanTime'
import type { Tone } from '../../ui/StatusChip'

/** Unknown tokens render as themselves in a `neutral` pill (the `ui/statusTone.ts` contract). `missed` is a
 *  failure (the scheduler never delivered it), toned as the Automations list's failing last run, never as a
 *  by-design skip. */
export const OUTCOME_TONE: Record<string, Tone> = {
  acted: 'success',
  noop: 'neutral',
  skipped: 'warning',
  missed: 'error',
  error: 'error',
}

/** The outcome's SHAPE on a feed row's state disc, beside `OUTCOME_TONE`: distinct at 11px, and `skipped`
 *  must not look like `noop`. Open on the wire, so an unknown outcome is `?`, never a throw. */
export const OUTCOME_GLYPH: Record<string, string> = {
  acted: '✓',
  noop: '○',
  skipped: '⊘',
  missed: '!',
  error: '✕',
}

/* HOW MUCH OF THE LEDGER A CRON KEEPS — `app/crons.py::RETENTION_MODES`, in words. No token means its bare
 * word: `none` still keeps every error and every unchosen skip (`ALWAYS_RECORDED_SKIP_REASONS`), `acted`
 * keeps missed rows too. `help` shows under the picker.
 * Named `..._OPTIONS`, not `RETENTION_MODES`: that name is the server's closed token set, a different shape.
 * All three reads (label, help, known?) live here, not at call sites. */

/* The tokens, mirroring `app/crons.py`'s `RETENTION_NONE`/`RETENTION_ACTED`/`RETENTION_ALL` — named because
 * the Runs tab branches on them. */
export const RETENTION_NONE = 'none'
export const RETENTION_ACTED = 'acted'
export const RETENTION_ALL = 'all'

/** `app/crons.py::DEFAULT_RETENTION_MODE`, mirrored. FLAT (see `CronCreate`): the sub-5-minute thrifty mode
 *  is the migration's for seeded crons; applied here it would discard a new cron's first firings. */
export const DEFAULT_CREATE_RETENTION_MODE = RETENTION_ALL

export const RETENTION_MODE_OPTIONS: readonly { value: string; label: string; help: string }[] = [
  {
    value: RETENTION_ALL,
    label: 'All',
    help: 'Every firing is logged.',
  },
  {
    value: RETENTION_ACTED,
    label: 'When it acted',
    help: 'Skips firings that found no work.',
  },
  {
    value: RETENTION_NONE,
    label: 'Errors only',
    help: 'Only failures and unexpected skips.',
  },
]

/** The picked mode's label, or the raw token for a mode this build has never heard of. */
export function retentionLabel(mode: string): string {
  return RETENTION_MODE_OPTIONS.find(m => m.value === mode)?.label ?? mode
}

/** `undefined` for an unrecognised mode — nothing honest to say. */
export function retentionHelp(mode: string): string | undefined {
  return RETENTION_MODE_OPTIONS.find(m => m.value === mode)?.help
}

/** `false` makes the page add the stored token as an extra option, instead of a select silently showing the first. */
export function isKnownRetentionMode(mode: string): boolean {
  return RETENTION_MODE_OPTIONS.some(m => m.value === mode)
}

/* ── What one firing came to, as a sentence ──────────────────────────────────────────────────────────
 * Every row reads as an actionable sentence ("Skipped — the task from the previous firing is still
 * open."), tokens still shown as pills. Source order:
 *   1. THE RUNTIME'S OWN `detail` for `acted`/`noop`/`skipped` — the only source of specifics (ids).
 *   2. A FALLBACK CLAUSE keyed on the tokens.
 * `missed` AND `error` NEVER USE THEIR DETAIL AS THE CLAUSE: it is not one (a cap addendum, an exception
 * `repr`), so it rides `note`. `missed` takes no clause at all (`OUTCOME_EXPLANATION`).
 */

/** The leading word — the outcome, said as an outcome rather than as a state name. */
const OUTCOME_WORD: Record<string, string> = {
  acted: 'Acted',
  noop: 'Nothing to do',
  skipped: 'Skipped',
  missed: 'Missed',
  error: 'Failed',
}

/** The clause for a skip whose runtime detail is missing, keyed on the marker
 *  (`app/crons.py::SKIP_REASONS`). */
const SKIP_CLAUSE: Record<string, string> = {
  already_running: 'the previous firing had not returned yet, so this one was dropped rather than run twice',
  previous_task_open: 'the work the last firing created is still open',
  disabled: 'the cron was switched off when this firing came due',
  below_min_period: "the schedule is faster than the minimum period",
}

/** A skip reason in words: its clause, or the token itself in words a reader can still place. Shared by the sentence
 *  (`readRun`) and the state mark's hover (`cronRunRow`), so neither shows the raw marker. */
export function skipClause(reason: string): string {
  return SKIP_CLAUSE[reason] ?? `it did not run (${reason})`
}

/** The clause for a non-skip outcome whose runtime detail is missing. */
const OUTCOME_CLAUSE: Record<string, string> = {
  acted: 'it did its work',
  noop: 'the activity looked and found no work waiting',
  error: 'the activity raised and did not finish',
}

/** An outcome every row of which would say the same thing: the row says the word alone, and the ledger says
 *  this ONCE (its legend, the state mark's hover), rather than truncating one long sentence per row. */
export const OUTCOME_EXPLANATION: Record<string, string> = {
  missed: 'The scheduler never delivered this firing; the ledger reconstructed it from the schedule afterwards.',
}

/** The two outcomes whose `detail` is never a clause — see the block comment above. */
const DETAIL_IS_NOT_A_CLAUSE = new Set(['missed', 'error'])

/* A detail that already contains an em-dash is not a clause either (the sentence spends its dash on the
 * outcome). Real: `node-liveness` and the no-op probe write such `acted` details
 * (`app/cron_system_activities.py`, `app/cron_activities.py`). It takes the fallback and rides `note`. */
function usableAsClause(detail: string, outcome: string): boolean {
  return detail !== '' && !DETAIL_IS_NOT_A_CLAUSE.has(outcome) && !detail.includes('—')
}

/* ── A DETAIL THAT IS NOT ONE LINE ─────────────────────────────────────────────────────────────────────
 * `lease-drain` writes a DECISION JOURNAL (line 1 a headline, then one decision per line, ≤2000 chars
 * API-side); a traceback has the same shape. In a table cell it either collapses into one run-on line or grows one
 * row to sixty lines. So the reading uses the HEADLINE ONLY (on `sentence` and `note`); `RunDrawer`'s
 * Detail block renders it in full. `moreLines` lets the row SAY more exists. A single-line detail is
 * exactly `run.detail.trim()` with `moreLines` 0. */
/** A detail's lines, SHARED WITH `RunDrawer` so the row's "+N more lines" and the drawer agree.
 *  Only TRAILING blanks are dropped (a final newline must not count as a line); inner blanks stay.
 *  A null `detail` reads as empty: the type says string, but one bad row must not take down the ledger. */
export function detailLines(detail: string | null | undefined): string[] {
  const lines = (detail ?? '').split(/\r?\n/)
  while (lines.length > 1 && lines[lines.length - 1].trim() === '') lines.pop()
  return lines
}

function splitDetail(detail: string): { headline: string; moreLines: number } {
  const lines = detailLines(detail)
  return { headline: lines[0].trim(), moreLines: lines.length - 1 }
}

export type RunReading = {
  /** Always ends in a full stop, names what happened, never a bare token. First line only. */
  sentence: string
  /** The runtime's text when NOT usable as the clause (exception `repr`, missed-rows addendum); else empty.
   *  First line only. */
  note: string
  /** Lines beyond the one used; non-zero means the full journal is in the drawer. */
  moreLines: number
}

/** `run` in words, by the source order above. Typed on the three deciding fields, so the Automations Runs tab
 *  reads its skips through it too. */
export function readRun(run: Pick<CronRun, 'outcome' | 'detail' | 'skip_reason'>): RunReading {
  const word = OUTCOME_WORD[run.outcome] ?? run.outcome
  /* THE HEADLINE, NOT THE WHOLE DETAIL (see `splitDetail`). */
  const { headline: detail, moreLines } = splitDetail(run.detail)

  if (usableAsClause(detail, run.outcome)) {
    // The runtime's sentence carries the specifics; nothing added, nothing dropped.
    return { sentence: `${word} — ${endWithStop(detail)}`, note: '', moreLines }
  }
  // The word alone: the explanation is the ledger's, said once (`OUTCOME_EXPLANATION`).
  if (!run.skip_reason && OUTCOME_EXPLANATION[run.outcome]) return { sentence: `${word}.`, note: detail, moreLines }
  const clause = run.skip_reason
    ? skipClause(run.skip_reason)
    : OUTCOME_CLAUSE[run.outcome] ?? 'the runtime recorded no reason'
  return { sentence: `${word} — ${clause}.`, note: detail, moreLines }
}

/* Keep the detail's own terminal mark (`.`/`?`/`!`), add a full stop only when it has none — never
 * rewrite or double the punctuation. */
function endWithStop(text: string): string {
  return /[.?!]$/.test(text) ? text : `${text}.`
}


/* ── The two spans, and what they mean when there is no span to state ────────────────────────────────
 * DURATION is `started_at → finished_at` (`app/models/automations.py::CronRun`), never measured from `fired_at`.
 * ABSENT CASES ARE STATED, NEVER ZERO: never started → `NO_VALUE`; in flight → `running…` (no live ticking
 * counter — the ledger records what HAPPENED).
 */

/** The one spelling of the in-flight duration, so the table and the drawer cannot word it differently. */
export const STILL_RUNNING = 'running…'

export type RunSpans = {
  /** `started_at → finished_at`: the work. `running…` while in flight, `NO_VALUE` if it never began. */
  duration: string
}

/** Typed on the three deciding columns rather than `CronRun`. */
export function readSpans(
  run: Pick<CronRun, 'fired_at' | 'started_at' | 'finished_at'>,
): RunSpans {
  const neverStarted = !run.started_at
  return {
    duration: neverStarted
      ? NO_VALUE
      : run.finished_at
        ? formatSpanBetween(run.started_at, run.finished_at)
        : STILL_RUNNING,
  }
}

