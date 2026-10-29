import { hashKey, isCancelledError, type QueryKey } from '@tanstack/react-query'
import { queryClient } from '../queryClient'

/* ── A CACHED READ for a surface that may mount without a `QueryClientProvider` ─────────────────────────────────
 * The task form (`components/tasks/TaskEditForm`) mounts in hosts with no provider, and the task detail page keys
 * it on `updated_at`, so every own-task event REMOUNTS it — and each mount used to re-read every catalogue it shows
 * (agent choices, models, roster, attachments, dependencies). This reads through the app's singleton
 * client imperatively (`fetchQuery`), which needs no provider, so a remount inside `staleTime` answers from cache,
 * two reads in flight for one key share one GET (React StrictMode's double mount, two hooks on one page), and a key
 * a `useQuery` caller already uses (`['task-models']`, `['project-members', id]` — `pages/ProjectEditPage.tsx`) is
 * one entry for both. The cached value is the RAW answer: callers shape it after, so a shared key holds one shape.
 * A failed read rejects and caches no data, so the next read asks again. Persistence is `queryClient.ts`'s
 * `UNPERSISTED_QUERIES` call, per key. */

/** Server-defined catalogues (the model tiers, the provider list) and a subject's attachment list, whose every
 *  in-console write forgets the entry (`lib/uploadTaskAttachment.ts::forgetSubjectAttachments`). Five minutes: long enough that
 *  every remount of a session hits cache, short enough that a deploy's new catalogue shows within one coffee. */
export const CATALOGUE_STALE_MS = 5 * 60_000

/** Lists other pages edit WITHOUT invalidating these keys (the agents catalog `pages/agents/AgentsPage.tsx`, the
 *  member and access pages): kept modest so an edit made there shows here within a minute. */
export const ROSTER_STALE_MS = 60_000

/* When each key's latest read was SENT (epoch ms). Sent, not answered: a read sent before an event can answer
 * after it with the world from before it, so only the send time says whether a read saw an event. */
const sentAt = new Map<string, number>()
// …dropped with its entry (a sign-in `clear()`, a test's), so a later entry for the key starts unsent.
queryClient.getQueryCache().subscribe(event => {
  if (event.type === 'removed') sentAt.delete(event.query.queryHash)
})

/* One retry on a CANCELLED read: a sign-in `clear()`, an org-scope `resetQueries` or a `forgetCachedRead` of a read
 * sent before its event cancels the fetch every caller is awaiting, and a plain `fetchQuery` (no observer) would
 * leave them on "loading" for good. The retry is a fresh read of the same key. */
export async function cachedRead<T>(queryKey: QueryKey, queryFn: () => Promise<T>, staleTime: number): Promise<T> {
  const hash = hashKey(queryKey)
  const read = () => queryClient.fetchQuery({
    queryKey,
    queryFn: () => {
      sentAt.set(hash, Date.now())
      return queryFn()
    },
    staleTime,
  })
  try {
    return await read()
  } catch (err) {
    if (isCancelledError(err)) return read()
    throw err
  }
}

/* Marks the entry stale WITHOUT refetching, so the next `cachedRead` of it asks the server whatever its
 * `staleTime`: the forced re-read after this surface's own write, or a live event that moved the view.
 * `heardAt` (epoch ms, the event's arrival): a read SENT at or after it already saw the event, so it is kept — two
 * readers that heard one event re-read once between them (the second joins the first's read, or takes its answer).
 * A read still in flight that was sent BEFORE it is cancelled, so the re-read reaches the server rather than joining
 * a read that predates the change (its awaiting callers retry: `cachedRead`). */
export function forgetCachedRead(queryKey: QueryKey, heardAt = Infinity): void {
  if ((sentAt.get(hashKey(queryKey)) ?? 0) >= heardAt) return
  if (queryClient.isFetching({ queryKey, exact: true }) > 0) void queryClient.cancelQueries({ queryKey, exact: true })
  void queryClient.invalidateQueries({ queryKey, exact: true, refetchType: 'none' })
}
