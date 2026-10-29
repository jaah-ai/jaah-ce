/* A LIST'S BULK PICK, HELD OUTSIDE REACT STATE. Held as page state, every tick re-rendered the whole
 * board — 75 feed rows of MUI per click — so rapid clicks queued behind long renders, the count lagged and picks
 * were lost. Here a tick subscribes to its own row (`useIsPicked`) and the count to the size, so a click
 * re-renders one tick and the bar; whatever else reads the pick subscribes to exactly what it shows.
 * Pure of the API client; keyed by row id, each pick holding its row whole (a bulk report names it). */
import { createExternalStore, useExternalStore } from './externalStore'

export type PickStore<T> = {
  /** The pick now. A new Map on every change, never mutated, so it is a valid `useSyncExternalStore` snapshot. */
  get: () => ReadonlyMap<number, T>
  /** Replace the pick from the current one. Returning `prev` changes nothing and notifies nobody. */
  update: (next: (prev: ReadonlyMap<number, T>) => ReadonlyMap<number, T>) => void
  subscribe: (listener: () => void) => () => void
}

const EMPTY: ReadonlyMap<number, never> = new Map<number, never>()

export function createPickStore<T>(): PickStore<T> {
  const picks = createExternalStore<ReadonlyMap<number, T>>(EMPTY)
  return { get: picks.get, subscribe: picks.subscribe, update: next => picks.set(next(picks.get())) }
}

/** Flip one row in or out of the pick. */
export function togglePick<T>(store: PickStore<T>, id: number, row: T): void {
  store.update(prev => {
    const next = new Map(prev)
    if (next.has(id)) next.delete(id)
    else next.set(id, row)
    return next
  })
}

/** Drop the whole pick. */
export function clearPicks<T>(store: PickStore<T>): void {
  store.update(prev => (prev.size === 0 ? prev : EMPTY))
}

/** The whole pick; re-renders on every change — for the bar, not for a row. */
export function usePicks<T>(store: PickStore<T>): ReadonlyMap<number, T> {
  return useExternalStore(store)
}

/** Whether one row is picked; re-renders only when THAT answer changes. */
export function useIsPicked<T>(store: PickStore<T>, id: number): boolean {
  return useExternalStore(store, picks => picks.has(id))
}

/** Whether anything is picked; re-renders only when the pick empties or stops being empty. */
export function usePicking<T>(store: PickStore<T>): boolean {
  return useExternalStore(store, picks => picks.size > 0)
}
