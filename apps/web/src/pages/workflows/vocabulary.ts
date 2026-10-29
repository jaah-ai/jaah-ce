/* ── What the Workflows surface says, in one file ──
 * The types tab, runs tab and detail page import their wordings from here so they never disagree
 * (precedent: `pages/cron/vocabulary.ts`). The DBOS status vocabulary is `lib/dbosStatus.ts`'s.
 */
import { NO_VALUE } from '../../lib/relativeTime'
import type { WorkflowRun, WorkflowType } from './api'

/* ── The window, said honestly ──
 * `runs` is never a lifetime total: it counts the last N runs the engine holds. The two surfaces fold
 * different populations with the same N — the types tab from the newest N across all workflows, a
 * workflow's page from its own newest N (`app/routes/workflows.py::WorkflowDetailOut`). So the scope is
 * part of the caption and deliberately not defaulted.
 */
export type WindowScope = 'all-workflows' | 'this-workflow'

const SCOPED_RUNS: Record<WindowScope, string> = {
  'all-workflows': 'runs across all workflows',
  'this-workflow': 'runs of this workflow',
}

export const windowCaption = (window: number, scope: WindowScope) =>
  `of the last ${window.toLocaleString()} ${SCOPED_RUNS[scope]}`

/** The compacted qualifier for the types list's narrow run-count slot (the long form wrapped to four
 *  lines and a 95px header at 1440px). It still names its population: the slot shows each workflow's
 *  share of a shared window, which differs from its own page's count. Declared beside `windowCaption`
 *  so neither prints the number without the population. */
export const windowCaptionShort = (window: number) =>
  `of last ${window.toLocaleString()}, all workflows`

/** The run count in one word. It travels on each feed row joined to `windowCaptionShort` in the slot's
 *  `title`; the two never ship apart. */
export const RUNS_COLUMN_HEADER = 'Runs'

/* ── The engine's absence ──
 * Two sentences: the type list loses only its numbers (the registry rows still list), so its banner
 * stops zeroed rows reading as "never ran"; the runs list loses everything.
 */
export const ENGINE_DOWN_TYPES =
  'Durable engine unreachable — run counts and last-run times are unavailable.'

export const ENGINE_DOWN_RUNS =
  'Durable engine unreachable — runs cannot be listed. Workflows may still be running.'

/* ── The two empty results ──
 * A filtered miss (the common case) must not read as "nothing has ever run". Same split as
 * `pages/audit/SessionsTab.tsx`; here the client knows its own filters.
 */
export const NO_RUNS_AT_ALL_TITLE = 'No workflow runs recorded'
export const NO_RUNS_MATCHED_TITLE = 'No runs match these filters'

/** The search field's copy: the server's `search` is a PREFIX match, so the label says so (as
 *  `pages/audit/SessionsTab.tsx`'s placeholder). The example is a whole current id
 *  (`task-done-<task>-<attempt>`, `app/durable/task_done.py::workflow_id_for`) — pasteable, and it shows
 *  the attempt counter that separates re-driven runs. */
export const SEARCH_LABEL = 'Run id starts with'
export const SEARCH_PLACEHOLDER = 'task-done-146-0'

/* ── What happened, in one line ──
 * Folds `error`, `output`, and the status when neither speaks into one sentence with a kind, so the cell
 * can style a failure differently. It never invents text: the only edit is unwrapping a plain-string dict
 * `repr`, and only when provably lossless.
 */
export type RunReading = {
  /** The line to render; never empty. */
  sentence: string
  /** `failure` is the workflow's own error text (error colour); `pending` has produced nothing yet (muted). */
  kind: 'failure' | 'result' | 'pending'
}

/* A flat dict `repr`, unwrapped only when provably the whole value. A string reads `key: value`
 * (`task_done`'s status map); a count reads `51 applied`, a list of strings its length, and a zero, an
 * empty list or `None` says nothing (`project_cron_schedules`'s `{'applied': 51, 'paused': 0, …, 'error': None}`
 * reads `51 applied`). The safety is reconstruction, not the regex: pairs are matched and joined back, and
 * the rewrite is used only if the join equals the original body. Nested dicts, any other value or a
 * `PAYLOAD_MAX_CHARS` truncation fall through to the raw string.
 */
const DICT_BODY = /^\{(.+)\}$/s
const QUOTED = String.raw`'(?:[^'\\]|\\.)*'`
const PAIR = new RegExp(String.raw`'([A-Za-z0-9_]+)': (?:'((?:[^'\\]|\\.)*)'|(-?\d+)|(None)|\[((?:${QUOTED}(?:, ${QUOTED})*)?)\])`, 'g')
const LIST_ITEM = new RegExp(QUOTED, 'g')

/** One pair as words, or null when it has nothing to say (a zero, an empty list, `None`). */
function readPair([, key, text, count, , list]: RegExpMatchArray): string | null {
  if (text !== undefined) return `${key}: ${text}`
  const label = key.replace(/_/g, ' ')
  if (count !== undefined) return Number(count) === 0 ? null : `${count} ${label}`
  if (list !== undefined && list !== '') return `${list.match(LIST_ITEM)?.length ?? 0} ${label}`
  return null
}

export function unwrapDictRepr(raw: string): string {
  const body = DICT_BODY.exec(raw)
  if (body === null) return raw
  const pairs = [...body[1].matchAll(PAIR)]
  if (pairs.length === 0) return raw
  if (pairs.map(p => p[0]).join(', ') !== body[1]) return raw
  const said = pairs.map(readPair).filter(part => part !== null)
  return said.length > 0 ? said.join(' · ') : 'Nothing changed.'
}

/** The first line of a multi-line value, marked when there was more; the drawer shows it whole. */
function firstLine(raw: string): string {
  const [head, ...rest] = raw.split('\n')
  return rest.some(line => line.trim() !== '') ? `${head.trim()} …` : head.trim()
}

/** One run's last column. Typed on the four fields it reads, so a test can pass just those. */
export function readRun(
  run: Pick<WorkflowRun, 'status' | 'error' | 'output' | 'queue_name'>,
): RunReading {
  if (run.error) return { sentence: firstLine(run.error), kind: 'failure' }
  if (run.output) return { sentence: firstLine(unwrapDictRepr(run.output)), kind: 'result' }

  /* No error and no output (workflows returning `None`, runs in flight): say what the absence means per
  status rather than blank or repeating the pill. */
  if (run.status === 'ENQUEUED') {
    return {
      sentence: run.queue_name
        ? `Waiting for a worker on the ${run.queue_name} queue.`
        : 'Waiting for a worker.',
      kind: 'pending',
    }
  }
  if (run.status === 'DELAYED') return { sentence: 'Scheduled to start later.', kind: 'pending' }
  if (run.status === 'PENDING') return { sentence: 'Not returned yet.', kind: 'pending' }
  if (run.status === 'SUCCESS') return { sentence: 'Finished, returning nothing.', kind: 'result' }
  if (run.status === 'CANCELLED') return { sentence: 'Cancelled before it finished.', kind: 'pending' }
  /* A fault status with no error text (`MAX_RECOVERY_ATTEMPTS_EXCEEDED`: the engine gave up). */
  return { sentence: 'Failed. No error recorded.', kind: 'failure' }
}

/* ── The task behind a run ──
 * `task_done(task_id: int)` and `generate_task_title(task_id: int)` take a task id as their only
 * positional argument, so it is read back from `input` to link `/tasks/{id}`. Safe because:
 *   · gated on a literal name set, so a new workflow is excluded until its signature is checked;
 *   · the pattern is anchored and requires the exact `repr` of a one-element tuple with empty kwargs;
 *   · `workflow_engine._payload` truncates at 500 chars from the front, so the prefix is intact.
 * Otherwise null and no link: an absent link is workable, a wrong one is not.
 */
const TASK_ARG_WORKFLOWS = new Set(['task_done', 'generate_task_title'])
const TASK_ARG = /^\{'args': \((\d+),\), 'kwargs': \{\}\}$/

export function taskIdOf(run: Pick<WorkflowRun, 'name' | 'input'>): number | null {
  if (!TASK_ARG_WORKFLOWS.has(run.name) || !run.input) return null
  const match = TASK_ARG.exec(run.input)
  return match === null ? null : Number(match[1])
}

/* ── The type row's own readings ─────────────────────────────────────────────────────────────────────
 */

/** For a workflow with no run in the window; distinct from a zero count ("no run recently"). */
export const NEVER_RAN = 'No run in the window'

/** `by_status` as ordered pairs, commonest first (ties by name, stable), skipping absent ones: the map is
 *  sparse by contract (`api.ts`). */
export function statusBreakdown(byStatus: Record<string, number>): { status: string; count: number }[] {
  return Object.entries(byStatus)
    .filter(([, count]) => count > 0)
    .map(([status, count]) => ({ status, count }))
    .sort((a, b) => b.count - a.count || a.status.localeCompare(b.status))
}

/** The run count, or `NO_VALUE` when the engine is unavailable: a zero it cannot vouch for is no
 *  measurement. */
export function readRunCount(type: WorkflowType, engineAvailable: boolean): string {
  return engineAvailable ? type.runs.toLocaleString() : NO_VALUE
}
