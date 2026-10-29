import { lazy, type ComponentType, type LazyExoticComponent } from 'react'

/* `React.lazy` for a ROUTE chunk that survives a failed fetch. `lazy` latches its first rejection for the life of
 * the page: a chunk request that hiccups (a dev server under load, a dropped connection) leaves that route on the
 * error boundary, whose Retry re-renders the same rejected promise, until a full reload. Retrying after growing
 * pauses turns a transient outage into a slow first paint — ONE retry 400ms later was not enough: a dev server
 * re-optimising its dependencies under CI load answers failures for seconds, and the e2e roster page latched
 * ("element(s) not found"). A chunk that stays unreachable past the last pause (a stale tab after a
 * deploy) still rejects into the boundary exactly as before, a few seconds later. `delays` is injectable so the
 * unit test does not sit out the real pauses. */
export const RETRY_DELAYS_MS: readonly number[] = [400, 1200, 3000]

export function lazyRetry<T extends ComponentType<any>>(
  load: () => Promise<{ default: T }>,
  delays: readonly number[] = RETRY_DELAYS_MS,
): LazyExoticComponent<T> {
  const attempt = (pauses: readonly number[]): Promise<{ default: T }> =>
    load().catch(error => {
      if (pauses.length === 0) throw error
      return new Promise<{ default: T }>((resolve, reject) => {
        setTimeout(() => attempt(pauses.slice(1)).then(resolve, reject), pauses[0])
      })
    })
  return lazy(() => attempt(delays))
}
