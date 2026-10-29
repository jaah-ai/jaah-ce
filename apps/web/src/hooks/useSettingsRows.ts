import { useCallback, useEffect, useRef, useState } from 'react'
import { apiDetail } from '../api'
import { useConfirm } from '../components/ConfirmDialog'
import { useRowActions } from './useBusyIds'

/* The controller of a Settings CRUD panel that owns its own fetch and writes (`settings/tags/TagsSection`
 * says why): the rows, the load / row-write failure banners, per-row busy flags and the confirm dialog.
 *
 * ONE REFETCH, MANY CALLERS (save, every row write), so responses interleave: a generation counter lets only
 * the newest write, and bumping it on unmount drops late resolves.
 * SHAPE-CHECKED when `shapeError` is given: `get<T>` is an unchecked cast, and these panels render as the
 * fallback when the rest of `/settings` fails — a non-list is a load failure, never a white screen.
 * `fetch` returns the rows plus an optional `apply` for whatever else the same response carries (it runs only
 * for the newest, well-shaped response). `fetch` is read from a ref, so an inline closure is fine. */
export interface SettingsRowsOptions {
  fetch: () => Promise<{ rows: unknown; apply?: () => void }>
  /** The load banner's fallback when the request fails. */
  failed: string
  /** The load banner when the rows are not a list; omitted = unchecked. */
  shapeError?: string
  /** A failed FIRST load still settles the rows to `[]` (ends the loading state). */
  emptyOnFailure?: boolean
  /** False = no request at all (a reader refused the read). */
  enabled?: boolean
}

export function useSettingsRows<T>({ enabled = true, ...options }: SettingsRowsOptions) {
  const [rows, setRows] = useState<T[] | null>(null)
  const [loadError, setLoadError] = useState('')
  const { busy: busyIds, error: rowError, setError: setRowError, run } = useRowActions<string>()
  const [confirm, confirmDialog] = useConfirm()
  const optionsRef = useRef(options)
  optionsRef.current = options
  const generation = useRef(0)

  const load = useCallback(async () => {
    if (!enabled) return
    const { fetch, failed, shapeError, emptyOnFailure } = optionsRef.current
    const mine = ++generation.current
    try {
      const { rows: next, apply } = await fetch()
      if (mine !== generation.current) return
      if (shapeError !== undefined && !Array.isArray(next)) {
        setRows([])
        setLoadError(shapeError)
        return
      }
      setRows(next as T[])
      apply?.()
      setLoadError('')
      // Clear a stale row failure here, or it sits over the successful write that followed.
      setRowError('')
    } catch (err) {
      if (mine !== generation.current) return
      if (emptyOnFailure) setRows(prev => prev ?? [])
      setLoadError(apiDetail(err, failed))
    }
  }, [enabled])

  useEffect(() => {
    void load()
    return () => {
      generation.current += 1
    }
  }, [load])

  /** One row write: busy while in flight, refetch after, the refusal over the list. */
  async function rowWrite(busyKey: string, fallback: string, act: () => Promise<unknown>) {
    await run(busyKey, fallback, async () => {
      await act()
      await load()
    })
  }

  return { rows, loadError, rowError, setRowError, load, busyIds, rowWrite, confirm, confirmDialog }
}
