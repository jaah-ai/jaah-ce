/* THE AUTOMATIONS SURFACE'S WORDS. What the server words (the schedule sentence, a status reason, a target's
 * skip reason, a source kind's label via `pages/triggers/vocabulary.ts::useSourceKindLabels`) is never respelled
 * here; this owns only what the server has no word for: the When picker's order, tones, and the two policy lines
 * the editor states under When (AC7.1, AC7.2). */

import { humanize } from '../../lib/humanize'
import type { Tone } from '../../ui/StatusChip'
import { OUTCOME_TONE, readRun } from '../cron/vocabulary'
import type { AutomationRun, When } from './api'

/** The When picker, in order: a schedule, then the event sources (`app/triggers.py::SOURCE_KINDS`). */
export const WHEN_CHOICES = ['schedule', 'webhook', 'github', 'gmail', 'slack', 'user_registered'] as const
export type WhenChoice = (typeof WHEN_CHOICES)[number]

export const WHEN_CHOICE_LABELS: Record<WhenChoice, string> = {
  schedule: 'Schedule',
  webhook: 'Webhook',
  github: 'GitHub',
  gmail: 'Email',
  slack: 'Slack',
  user_registered: 'New user',
}

/** AC7.1: one missed-run policy, stated where the schedule is set. */
export const MISSED_RUNS_POLICY = 'Missed runs while down are skipped; the next run happens on schedule.'
/** AC7.2 / D10: overlap is decided per target — a schedule skips it (`automation_fire.py::_fire_schedule`)... */
export const OVERLAP_POLICY = 'A target whose previous task is still open is skipped.'
/** ...an event queues it at the Concurrency cap and skips it only once the queue is full (`_fire_event`). */
export const EVENT_OVERLAP_POLICY =
  'A target at its Concurrency cap queues the new task until one finishes; a full queue skips it.'

/** Above this many resolved targets the editor says how many tasks one run files (save is never blocked). */
export const MANY_TARGETS = 100

export const STATUS_TONE: Record<string, Tone> = { active: 'success', paused: 'neutral', failing: 'error' }
export const STATUS_LABELS: Record<string, string> = { active: 'Active', paused: 'Paused', failing: 'Failing' }
/** The status mark's glyph, in the feed's vocabulary (`ui/feed/types.ts::FeedIndicator`). */
export const STATUS_GLYPH: Record<string, string> = { active: '●', paused: '■', failing: '✕' }

/** The list's last-run column: the server's one outcome word (`automation_service.LAST_RUN_OUTCOMES`) as a
 *  glyph (the feed's vocabulary), a tone, and the word an operator reads on hover. A word this table does not
 *  know (a newer server's) draws neutral under its own spelling — never as a success it did not claim. */
export const LAST_RUN_OUTCOME: Record<string, { glyph: string; tone: Tone; label: string }> = {
  filed: { glyph: '✓', tone: 'success', label: 'Filed' },
  ran: { glyph: '✓', tone: 'success', label: 'Ran' },
  noop: { glyph: '○', tone: 'neutral', label: 'Nothing to do' },
  // Passed over by design (filtered, deduped, already running): nothing ran, like `noop`.
  skipped: { glyph: '○', tone: 'neutral', label: 'Skipped' },
  // An occurrence the engine never delivered is a failure, drawn as the failing status disc draws one.
  missed: { glyph: STATUS_GLYPH.failing, tone: STATUS_TONE.failing, label: 'Missed' },
  error: { glyph: STATUS_GLYPH.failing, tone: STATUS_TONE.failing, label: 'Error' },
  // In flight or queued behind a target's cap: not yet an outcome.
  pending: { glyph: '»', tone: 'info', label: 'Pending' },
}
export const lastRunOutcome = (outcome: string) =>
  LAST_RUN_OUTCOME[outcome] ?? { glyph: '○', tone: 'neutral' as Tone, label: humanize(outcome) }

/** A firing the server dates but kept no row of (`last_run_outcome` null), worded where the schedule's retention mode
 *  explains the gap (`app/crons.py::should_record`): `acted` drops a `noop` (and the rare
 *  `below_min_period` skip, which did nothing either), so the firing had nothing to do;
 *  `none` drops every success, so it did not fail. Null where nothing explains it (an `all` row's pruned history,
 *  an event): the age then stands alone. */
export function unrecordedOutcome(when: When): { glyph: string; tone: Tone; label: string; data: string } | null {
  if (when.kind !== 'schedule') return null
  if (when.retention_mode === 'acted') return { ...LAST_RUN_OUTCOME.noop, data: 'noop' }
  if (when.retention_mode === 'none') return { glyph: '○', tone: 'neutral', label: 'Not recorded', data: 'unrecorded' }
  return null
}

/** The list's When column: the server's compact phrase for a regular schedule, else its full sentence; an
 *  event's `whenSummary` is already short. */
export function whenShort(when: When, sourceLabel: (kind: string) => string): string {
  if (when.kind === 'schedule') return when.schedule_short || whenSummary(when, sourceLabel)
  return whenSummary(when, sourceLabel)
}

export const CAUSE_LABELS: Record<string, string> = {
  schedule: 'Schedule tick',
  event: 'Event delivery',
  manual: 'Run now',
}

/** A schedule run's outcome (`app/crons.py::RUN_OUTCOMES`); an event run carries the server's own
 *  `outcome_label`. `acted` is "Filed" when it filed tasks, else the generic "Ran" (a system action). */
const RUN_OUTCOME_LABELS: Record<string, string> = {
  acted: 'Ran',
  noop: 'Nothing to do',
  skipped: 'Skipped',
  missed: 'Missed',
  error: 'Error',
}

/** A run's own outcome tokens that are a failure: a schedule's `error` and `missed` (`app/crons.py::RUN_OUTCOMES`),
 *  an event's `failed` delivery (`app/triggers.py`). */
const FAILED_RUN_OUTCOMES = new Set(['error', 'missed', 'failed'])

/** A run's chip tone (Runs tab and run page): a failed run, or one with an errored target, in the list's failing
 *  tone (the same read `LAST_RUN_OUTCOME` gives the list's last-run column), so a missed run never reads like a
 *  by-design skip; anything else as Audit > Schedule tones it (`cron/vocabulary.ts::OUTCOME_TONE`), so a skip and
 *  a run that did work differ at a glance. */
export function runOutcomeTone(run: AutomationRun): Tone {
  if (FAILED_RUN_OUTCOMES.has(run.outcome) || run.target_counts.error) return STATUS_TONE.failing
  return OUTCOME_TONE[run.outcome] ?? 'neutral'
}

/** Worded as every outcome is ("Missed", "Skipped"): a token outside the table (an older or newer server's,
 *  or an event's raw state) reads in sentence case, never as the lowercase wire token. */
export function runOutcomeLabel(run: AutomationRun): string {
  if (run.outcome_label) return humanize(run.outcome_label)
  if (run.outcome === 'acted' && run.target_counts.filed) return 'Filed'
  if (run.outcome === 'noop' && run.targets_total > 0) return 'Nothing filed'
  return RUN_OUTCOME_LABELS[run.outcome] ?? humanize(run.outcome)
}

/** The words of a line, ignoring case and its closing stop, so "Skipped." matches the chip's "Skipped". */
const wordsOf = (text: string) => text.trim().replace(/[.?!]+$/, '').toLowerCase()

/** The line under the chip for a run without targets: a schedule skip worded as Audit > Schedule words it
 *  (`readRun`), else the stored headline (an event delivery's skip reason is already the server's sentence). Never a
 *  line that only repeats the chip: an empty detail, or one that is the chip's word. */
export function runLine(run: AutomationRun, headline: string): string {
  if (run.outcome === 'skipped' && !run.outcome_label && (headline !== '' || run.skip_reason)) {
    const reading = readRun({ outcome: run.outcome, detail: headline, skip_reason: run.skip_reason ?? null })
    return reading.note ? `${reading.sentence} ${reading.note}` : reading.sentence
  }
  return wordsOf(headline) === wordsOf(runOutcomeLabel(run)) ? '' : headline
}

/** Per-target outcome words and tones (`app/automations.py::RUN_TARGET_OUTCOMES`). */
export const TARGET_OUTCOME_LABELS: Record<string, string> = {
  filed: 'Filed',
  skipped: 'Skipped',
  error: 'Error',
  not_reached: 'Not reached',
  queued: 'Queued',
}
export const TARGET_OUTCOME_TONE: Record<string, Tone> = {
  filed: 'success',
  skipped: 'warning',
  error: 'error',
  not_reached: 'warning',
  queued: 'info',
}
/** The order a run's outcome groups are drawn in: what needs attention first (AC4.6). */
export const TARGET_OUTCOME_ORDER = ['error', 'skipped', 'not_reached', 'queued', 'filed'] as const

/** Filed first (the run's yield), then attention first; an unknown outcome last. */
function summaryOrder(counts: AutomationRun['target_counts']): string[] {
  const rest = TARGET_OUTCOME_ORDER.filter(o => o !== 'filed') as string[]
  const known = ['filed', ...rest]
  const present = Object.keys(counts)
  return [...known.filter(o => present.includes(o)), ...present.filter(o => !known.includes(o)).sort()]
}

/** "480 filed · 15 skipped: previous still open · 5 error" — one segment per outcome and reason, largest reason
 *  first within an outcome. */
export function targetSummary(run: AutomationRun): string {
  const segments: string[] = []
  for (const outcome of summaryOrder(run.target_counts)) {
    const word = (TARGET_OUTCOME_LABELS[outcome] ?? outcome).toLowerCase()
    const reasons = Object.entries(run.target_counts[outcome]).sort((a, b) => b[1] - a[1])
    for (const [reason, n] of reasons) segments.push(reason ? `${n} ${word}: ${reason}` : `${n} ${word}`)
  }
  return segments.join(' · ')
}

const strings = (value: unknown): string[] =>
  Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string' && v !== '') : []

/** The When choice an automation's When is. */
export function whenChoiceOf(when: When): WhenChoice {
  if (when.kind === 'schedule') return 'schedule'
  return (WHEN_CHOICES as readonly string[]).includes(when.source_kind) ? (when.source_kind as WhenChoice) : 'webhook'
}

/** When, human readable: the server's schedule sentence, or the source with what it listens for
 *  ("GitHub: pull_request opened", "Slack: C0123ABCD", "Webhook"). `sourceLabel` is the server's word for the
 *  kind. */
export function whenSummary(when: When, sourceLabel: (kind: string) => string): string {
  if (when.kind === 'schedule') return when.schedule_human || when.cron_expression || '—'
  const label = sourceLabel(when.source_kind)
  const config = when.source_config ?? {}
  if (when.source_kind === 'github') {
    const events = strings(config.events)
    const actions = strings(config.actions)
    if (events.length === 0) return label
    return `${label}: ${events.join(', ')}${actions.length > 0 ? ` ${actions.join(', ')}` : ''}`
  }
  if (when.source_kind === 'slack') {
    const channels = strings(config.channel_ids)
    return channels.length > 0 ? `${label}: ${channels.join(', ')}` : label
  }
  return label
}
