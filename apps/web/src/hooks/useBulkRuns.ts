import { useCallback, useRef, useState } from 'react'

import type { BulkAction } from '../ui/BulkActionBar'
import {
  omitKey,
  runOverSelection,
  type BulkReport,
  type BulkRowNote,
  type BulkRun,
  type BulkVerb,
} from '../lib/bulkRun'

/* The stateful half of a page's bulk verbs (`lib/bulkRun.ts` is the fan-out, `ui/BulkRunReports.tsx` the
 * banners): per-verb runs in flight, per-verb reports, and the BATCH rule — the page's selection is
 * cleared only when every run in a batch of overlapping runs was clean, so the refused rows stay picked
 * for a retry. `Id` is the page's closed set of verb ids: each id keys three MAPS (button, greying run,
 * report), so a drifted literal would fail silently — no progress, no report, double-firable mid-run. */
export function useBulkRuns<Row, Id extends string = string>({
  identity,
  failureLine,
  onBatchClean,
}: {
  /** The row's name as the report speaks it. */
  identity: (row: Row) => string
  /** One refused row's report line; see `lib/bulkRun.ts::defaultFailureLine` for the default. */
  failureLine?: (row: Row, err: unknown) => string
  /** Fired by the last run standing when EVERY run in its batch was clean (the page clears its selection). */
  onBatchClean?: () => void
}): {
  /** Runs in flight by verb — also the "is this verb busy" answer, not a second flag. */
  runs: Record<string, BulkRun>
  /** Each fan-out's report by verb: overlapping runs each owe their own. */
  reports: Record<string, BulkReport>
  runBulk: <T>(
    id: Id,
    verb: BulkVerb,
    rows: readonly Row[],
    run: (row: Row) => Promise<T>,
    /** This verb's per-row 2xx remark (see `BulkRowNote`). */
    inspect?: (row: Row, result: T) => BulkRowNote | null,
  ) => Promise<void>
  /** Why a verb is grey while its own run goes, with the live count (`BulkActionBar` forbids reasonless
   *  disabled controls). Only the pressed verb greys: a second copy would re-fire over rows the
   *  first still answers for. */
  runReason: (id: Id) => string | undefined
  /** One `BulkAction` over this hook's state: grey while its own run goes or nothing is eligible (saying
   *  which), one press = optional confirm → fan-out → `after`. The page keeps its words and its confirm payload. */
  verb: <T>(spec: BulkVerbSpec<Row, Id, T>) => BulkAction
} {
  const [reports, setReports] = useState<Record<string, BulkReport>>({})
  const [runs, setRuns] = useState<Record<string, BulkRun>>({})
  /* The batch in flight: a ref because `runBulk` reads it after an await (captured state would be stale).
     `live` counts invocations; `clean` spans the batch and resets when one opens. */
  const batch = useRef({ live: 0, clean: true })

  const runBulk = useCallback(
    async <T,>(
      id: Id,
      verb: BulkVerb,
      rows: readonly Row[],
      run: (row: Row) => Promise<T>,
      inspect?: (row: Row, result: T) => BulkRowNote | null,
    ) => {
      /* The progress line shows `0 of N` before the first request, or the longest gap looks frozen. */
      /* A batch opens when nothing else runs; `clean` is about the whole batch. */
      if (batch.current.live === 0) batch.current.clean = true
      batch.current.live += 1
      setRuns(prev => ({ ...prev, [id]: { verb: verb.doing, done: 0, total: rows.length } }))
      setReports(prev => omitKey(prev, id))
      /* The `prev[id]` test is defensive: it stops a late callback resurrecting a finished run's line. */
      const { done, failures, notes, degraded } = await runOverSelection(rows, run, {
        identity,
        failureLine,
        doneWord: verb.done.toLowerCase(),
        onProgress: settled =>
          setRuns(prev => (prev[id] ? { ...prev, [id]: { ...prev[id], done: settled } } : prev)),
        inspect,
      })
      batch.current.live -= 1
      /* A degraded run keeps the selection like a refused one: those are the rows to look at again. */
      if (failures.length > 0 || degraded) batch.current.clean = false
      setRuns(prev => omitKey(prev, id))
      setReports(prev => ({
        ...prev,
        [id]: { verb: verb.done, done, total: rows.length, failures, notes, degraded },
      }))
      /* Cleared only when EVERY run in the batch was clean, and only by the last run standing: a sibling in
         flight still acts on those rows, and a clean run finishing last must not drop a refused sibling's
         retry set. */
      if (batch.current.live === 0 && batch.current.clean) onBatchClean?.()
    },
    [identity, failureLine, onBatchClean],
  )

  const runReason = useCallback((id: Id): string | undefined => {
    const live = runs[id]
    return live ? `${live.verb} ${live.done} of ${live.total}…` : undefined
  }, [runs])

  const verb = useCallback(<T,>({
    id, label, icon, destructive, rows, noneWhy, doing, done, run, inspect, confirm, after,
  }: BulkVerbSpec<Row, Id, T>): BulkAction => ({
    id,
    label,
    icon,
    destructive,
    disabled: !!runs[id] || rows.length === 0,
    disabledReason: runReason(id) ?? (rows.length === 0 ? noneWhy : undefined),
    onRun: () => {
      void (async () => {
        if (confirm && !(await confirm())) return
        await runBulk(id, { doing, done }, rows, run, inspect)
        await after?.()
      })()
    },
  }), [runs, runReason, runBulk])

  return { runs, reports, runBulk, runReason, verb }
}

/** A page's bulk verb, as `useBulkRuns().verb` builds it into a `BulkAction`. */
export type BulkVerbSpec<Row, Id, T> = Pick<BulkAction, 'label' | 'icon' | 'destructive'> & {
  id: Id
  /** The eligible rows, derived from the selection as it is NOW. */
  rows: readonly Row[]
  /** Why the verb is grey when no picked row is eligible. */
  noneWhy?: string
  doing: string
  done: string
  run: (row: Row) => Promise<T>
  inspect?: (row: Row, result: T) => BulkRowNote | null
  /** The page's own confirm, resolving false to drop the press. */
  confirm?: () => Promise<boolean>
  /** After the fan-out settles (a refetch). */
  after?: () => Promise<unknown> | void
}
