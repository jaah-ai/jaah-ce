import { useCallback, useState } from 'react'

import { apiDetail } from '../api'
import { withAdded, withRemoved } from '../lib/sets'

/* Per-row "a write is in flight" tracking. A Set, not a scalar: two rows can be in flight, and a scalar
 * would let one completion clear another's flag. `withBusy` marks before the work and clears in `finally`
 * (a rejection releases the row and still propagates).
 * `withBusy` is stable across renders (depends only on the setter), safe in dependency lists. */
export function useBusyIds<K = string>(): [
  busy: ReadonlySet<K>,
  withBusy: (id: K, fn: () => Promise<void>) => Promise<void>,
] {
  const [busyIds, setBusyIds] = useState<Set<K>>(new Set())

  const withBusy = useCallback((id: K, fn: () => Promise<void>): Promise<void> => {
    setBusyIds(prev => withAdded(prev, id))
    return fn().finally(() => setBusyIds(prev => withRemoved(prev, id)))
  }, [])

  return [busyIds, withBusy]
}

/* A list page's row actions: `useBusyIds` plus the ONE action error shown over the list. `run` clears the
 * error (and the notice), marks the row busy, runs the write, and routes a rejection through `apiDetail` to
 * `error` (it never rejects). Given `done`, a landed write's result names the success `notice` (`setNotice('')`
 * dismisses it). `run` is stable across renders. A write whose refusal goes elsewhere (a notice, a form) keeps `withBusy`. */
export function useRowActions<K = string>(): {
  busy: ReadonlySet<K>
  withBusy: (id: K, fn: () => Promise<void>) => Promise<void>
  error: string
  setError: (message: string) => void
  notice: string
  setNotice: (message: string) => void
  run: <T>(id: K, fallback: string, write: () => Promise<T>, done?: (result: T) => string) => Promise<void>
} {
  const [busy, withBusy] = useBusyIds<K>()
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const run = useCallback(<T>(id: K, fallback: string, write: () => Promise<T>, done?: (result: T) => string) =>
    withBusy(id, async () => {
      setError('')
      setNotice('')
      try {
        const result = await write()
        if (done) setNotice(done(result))
      } catch (err) {
        setError(apiDetail(err, fallback))
      }
    }), [withBusy])

  return { busy, withBusy, error, setError, notice, setNotice, run }
}
