/* ONE VALUE HELD OUTSIDE REACT, read by any component through `useSyncExternalStore`. The console keeps a dozen
 * of these (the org a tab acts as, the board's scope, the rewriter's version…): a value two components share with
 * no parent to hold it, or one that non-React code sets. Each used to hand-roll the same listener set, subscribe
 * and notify loop; this is that plumbing once, so a store is its value and its hook alone.
 *
 * `set` skips an unchanged value (`Object.is`): `useSyncExternalStore` ignores a notify whose snapshot is unchanged
 * anyway, so nothing observable moves. Listeners are notified from a copy, so one may unsubscribe mid-notify. */
import { useSyncExternalStore } from 'react'

export type ExternalStore<T> = {
  get(): T
  set(next: T): void
  subscribe(listener: () => void): () => void
  /** Test seam: back to `initial`, with nobody listening. */
  reset(): void
}

export function createExternalStore<T>(initial: T): ExternalStore<T> {
  let value = initial
  const listeners = new Set<() => void>()
  return {
    get: () => value,
    set: next => {
      if (Object.is(next, value)) return
      value = next
      for (const listener of [...listeners]) listener()
    },
    subscribe: listener => {
      listeners.add(listener)
      return () => {
        listeners.delete(listener)
      }
    },
    reset: () => {
      value = initial
      listeners.clear()
    },
  }
}

/** The store's value as React state — or `select`'s view of it, re-rendering only when THAT answer changes.
 *  `select` must return a primitive or a stable reference: a fresh object per call is a render loop. */
export function useExternalStore<T, S = T>(
  store: Pick<ExternalStore<T>, 'get' | 'subscribe'>,
  select?: (value: T) => S,
): S {
  const snapshot = () => (select ? select(store.get()) : (store.get() as unknown as S))
  return useSyncExternalStore(store.subscribe, snapshot, snapshot)
}
