import type { QueryClient } from '@tanstack/react-query'
import type { PipelineTask } from '../types'
import { boardRowsEpoch } from './taskPageSeed'

/* ── The board's LAST FIRST PAGE, painted at once on a reload (`pages/Tasks.tsx`) ─────────────────────────────
 * The list is the one read the board waits on, and a reload used to show no row until it answered behind every
 * other load-time read. The board now keeps its newest first pages in the react-query cache, which `main.tsx`
 * persists, so a reload — or a click back to a project seen before — paints those rows straight away and the list's
 * own read replaces them when it lands.
 *  - One entry PER URL it was read with, the newest `TASK_LIST_SEED_URLS` kept: a filter, scope or window not
 *    among them paints nothing old (the board shows its skeleton, never another scope's rows).
 *  - Freshness is the list's: the read always runs, and its answer wins (the seed is painted only while no
 *    answer has been).
 *  - A read started under one identity and landing after a change (sign-out, a switch, entering or leaving act-as) is
 *    never saved: the board passes the epoch it started under (`taskPageSeed.ts::boardRowsEpoch`), since this write
 *    is the board's own and react-query's clear cannot cancel it.
 *  - It rides the cache's own rules: cleared on an identity change, not saved or restored while acting as an org,
 *    discarded across a deploy (`queryClient.ts`, `main.tsx`). A day's lifetime, the persisted cache's, so leaving
 *    the board for a while before reloading on it still finds it.
 *  - A deleted task leaves it at once (`dropFromTaskListSeed`, called by `forgetDeletedTasks`), so returning to the
 *    board never flashes a row the operator just deleted. */

export const TASK_LIST_SEED_KEY = ['task-list-seed'] as const
/** The seed's lifetime without a reader, registered on the client BEFORE the persisted cache is restored
 *  (`queryClient.ts`), since a restored entry keeps the lifetime it was restored with. */
export const TASK_LIST_SEED_GC_MS = 24 * 60 * 60_000

/** How many first pages the seed keeps (the persisted cache lives in `localStorage`, so a handful). */
export const TASK_LIST_SEED_URLS = 6

type TaskListSeed = { url: string; rows: PipelineTask[] }

/* The entries, newest first. A cache persisted before entries were kept per URL holds one bare entry: read as none;
   so is a stored entry of any other shape. */
function seedEntries(client: QueryClient): TaskListSeed[] {
  const held = client.getQueryData<unknown>(TASK_LIST_SEED_KEY)
  if (!Array.isArray(held)) return []
  return held.filter(
    (entry): entry is TaskListSeed =>
      typeof entry === 'object' && entry !== null && typeof entry.url === 'string' && Array.isArray(entry.rows),
  )
}

export function saveTaskListSeed(client: QueryClient, url: string, rows: PipelineTask[], startedAt: number): void {
  if (startedAt !== boardRowsEpoch()) return
  const others = seedEntries(client).filter(entry => entry.url !== url)
  client.setQueryData<TaskListSeed[]>(TASK_LIST_SEED_KEY, [{ url, rows }, ...others].slice(0, TASK_LIST_SEED_URLS))
}

export function dropFromTaskListSeed(client: QueryClient, ids: readonly number[]): void {
  const entries = seedEntries(client)
  const gone = new Set(ids)
  if (!entries.some(entry => entry.rows.some(row => gone.has(row.id)))) return
  client.setQueryData<TaskListSeed[]>(
    TASK_LIST_SEED_KEY,
    entries.map(entry => ({ ...entry, rows: entry.rows.filter(row => !gone.has(row.id)) })),
  )
}

/** The rows last read for exactly `url`, or undefined. */
export function readTaskListSeed(client: QueryClient, url: string): PipelineTask[] | undefined {
  return seedEntries(client).find(entry => entry.url === url)?.rows
}
