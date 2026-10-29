import { ApiError } from '../api'
import { refusalMessage } from './erase'

/* ── FANNING ONE VERB OUT OVER A SELECTION ─────────────────────────────────────────────────────────
 *
 * The pure half of a page's bulk verbs (the stateful half is `hooks/useBulkRuns.ts`, the banners are
 * `ui/BulkRunReports.tsx`). Generic over the row — a pool slot (`pages/Worktrees.tsx`), a node — so each
 * page brings only its row identity and its per-row calls.
 *
 * NO BULK ENDPOINT, by design: every verb is authorized PER ROW on the server (the pool's `POST
 * /api/commands` and `release` re-check `worktree_editable_clause`; a node's verbs check its own row), and a
 * batch route would weaken that and flatten per-row answers into one.
 *
 * ALL AT ONCE (operator decision): the run is as long as its slowest row; node-side work
 * serializes where it must.
 *
 * NOTHING THROWS OUT OF HERE: a failure is a RESULT, so one refusal cannot lose every other row's answer.
 *
 * The report reads in ROW order (each refusal written into its row's index); the counter counts rows
 * SETTLED, so it moves even when every row is refused.
 *
 * `notes`: a success can owe a line — the pool's Delete answers 200 even when the node could not remove
 * the directory (row gone, orphan on disk). A verb may inspect its own result and add a note; one flag
 * says whether any note degrades the report to amber. Each note carries its own word, so severity
 * survives greyscale and screen readers.
 */
export type BulkOutcome = { done: number; failures: string[]; notes: string[]; degraded: boolean }

/** One row's 2xx remark: `line` is the full sentence (the verb composes it), `degraded` pulls the report
 *  to a warning. `null` adds nothing. */
export type BulkRowNote = { line: string; degraded: boolean }

/** A verb in both tenses, as one value, so no call site forgets the progress line's word. */
export type BulkVerb = { doing: string; done: string }

/** One fan-out in flight; `done` counts rows SETTLED. */
export type BulkRun = { verb: string; done: number; total: number }

/** One fan-out's result, held until the same verb is pressed again. */
export type BulkReport = {
  verb: string
  done: number
  total: number
  failures: string[]
  notes: string[]
  degraded: boolean
}

/** What a fan-out needs from its page besides the rows and the call. */
export type BulkRunOptions<Row, T> = {
  /** The row's name as the report speaks it (`lib/poolSlot.ts::slotIdentity` for a slot). */
  identity: (row: Row) => string
  /** One refused row's report line; defaults to `defaultFailureLine` (the server's words under the name). */
  failureLine?: (row: Row, err: unknown) => string
  /** The verb's past tense, lower-case, for the "could not read its result" note (`deleted`, `enabled`). */
  doneWord?: string
  onProgress?: (settled: number) => void
  /** This verb's per-row 2xx remark (see `BulkRowNote`); only a verb whose 200 can owe one has it. */
  inspect?: (row: Row, result: T) => BulkRowNote | null
}

/* How many rows a bulk confirm names before "… and N more" (`ConfirmDialog`'s `identity`), so the
   modal never scrolls. */
const IDENTITY_CAP = 8

/** A bulk confirm NAMES its rows, capped: the page brings its row word (`BulkRunOptions.identity`). */
export function bulkIdentity<Row>(rows: readonly Row[], identity: (row: Row) => string): string {
  const named = rows.slice(0, IDENTITY_CAP).map(identity).join(', ')
  return rows.length > IDENTITY_CAP ? `${named} … and ${rows.length - IDENTITY_CAP} more` : named
}

/** The server's own words, always under the row's name (`detail` names a reason, never a row). A NODE
 *  refusal is a plain `Error` (the pool's `runSlotCommand`), already prefixed with the row — pushed
 *  verbatim, never re-prefixed. A CP refusal is an `ApiError`, whose `message` is only the `detail` when
 *  that is a STRING — structured bodies (the release guard's `{reason, session_status, message}`,
 *  `app/routes/pool_slots.py`) came through as bare `HTTP 409`. So `refusalMessage` first: it descends
 *  into the body (`lib/erase.ts` gives the order), subsumes `apiDetail`, and matches
 *  `WorktreePool.tsx::handleRelease`, so both surfaces read one 409 alike. */
export function defaultFailureLine<Row>(identity: (row: Row) => string, row: Row, err: unknown): string {
  const said = refusalMessage(err)
  const nodeReason = !(err instanceof ApiError) && err instanceof Error ? err.message.trim() : ''
  if (said) return `${identity(row)}: ${said}`
  if (nodeReason) return nodeReason
  return `${identity(row)}: ${err instanceof ApiError ? err.message : 'failed'}`
}

/* Generic over one row's answer; only `inspect` sees `T`, so result meaning stays at the verb's call site. */
export async function runOverSelection<Row, T>(
  rows: readonly Row[],
  run: (row: Row) => Promise<T>,
  options: BulkRunOptions<Row, T>,
): Promise<BulkOutcome> {
  const { identity, onProgress, inspect, doneWord = 'done' } = options
  const failureLine = options.failureLine
    ?? ((row: Row, err: unknown) => defaultFailureLine(identity, row, err))
  /* Refusals and notes land by row index (the fleet answers in any order), so the report reads in row order. */
  const failures: (string | null)[] = rows.map(() => null)
  const notes: (BulkRowNote | null)[] = rows.map(() => null)
  let settled = 0
  /* Every `run` fires this tick; nothing rejects, so `Promise.all` waits for every answer. */
  await Promise.all(rows.map(async (row, index) => {
    let result: T
    try {
      result = await run(row)
    } catch (err) {
      failures[index] = failureLine(row, err)
      settled += 1
      onProgress?.(settled)
      return
    }
    /* `inspect` is OUTSIDE the request's `try`: a reader throwing on an odd body (`api()` gives `undefined`
       for 204, `null` for an unparseable 200 — both reachable mid-deploy) must not report a completed
       verb as failed. A note can never unmake a success; the reader's complaint becomes the note. */
    try {
      notes[index] = inspect?.(row, result) ?? null
    } catch (err) {
      notes[index] = {
        line: `${identity(row)}: ${doneWord}, but its result could not be read ` +
          `(${err instanceof Error ? err.message : 'unexpected body'})`,
        degraded: true,
      }
    }
    settled += 1
    onProgress?.(settled)
  }))
  const refused = failures.filter((line): line is string => line !== null)
  const said = notes.filter((note): note is BulkRowNote => note !== null)
  return {
    done: rows.length - refused.length,
    failures: refused,
    notes: said.map(note => note.line),
    // Any degraded row degrades the run: the one row nobody looks at is what the amber is for.
    degraded: said.some(note => note.degraded),
  }
}

/** One key out of a by-verb map, without mutating the map React is rendering. */
export function omitKey<T>(map: Record<string, T>, key: string): Record<string, T> {
  if (!(key in map)) return map
  const next = { ...map }
  delete next[key]
  return next
}
