import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { apiDetail, ApiError } from '../api'
import { type ConfirmOptions, useConfirm } from '../components/ConfirmDialog'

/* An id-keyed edit page's load and delete (`UserEditPage`,
 * `GroupEditPage`, `credentials/CredentialEditPage`, `customers/CustomerPage`). `id === null` is create mode:
 * nothing is fetched, never "loading". A new id re-shows loading; a landed load clears not-found/error.
 * `fetch`/`onLoad` ride a ref, so an inline closure does not re-fire the load. A superseded or unmounted
 * read writes NOTHING (generation fence): a late response must not re-seed the form over typing.
 * `fixedLoadError` shows `loadFallback` instead of the server's detail. `reload` re-reads without the
 * skeleton. Delete asks via `components/ConfirmDialog` (never `window.confirm`: a suppressed native dialog
 * fires without intent; the page renders `confirmDialog` once), then `replace`s to `listTo`, since this URL
 * now names a missing row; a page that deletes passes `listTo` and `deleteFallback`. */
export function useEditRecord<T>(
  id: string | null,
  fetch: (id: string) => Promise<T>,
  onLoad: (row: T) => void,
  opts: {
    loadFallback: string; fixedLoadError?: boolean; notFoundStatuses?: readonly number[]
    listTo?: string; deleteFallback?: string
  },
) {
  const navigate = useNavigate()
  const [confirm, confirmDialog] = useConfirm()
  const [loading, setLoading] = useState(id !== null)
  const [notFound, setNotFound] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState('')
  const live = useRef({ fetch, onLoad, opts })
  live.current = { fetch, onLoad, opts }
  const gen = useRef(0)

  const load = useCallback(async (key: string) => {
    const mine = ++gen.current
    const { loadFallback, fixedLoadError, notFoundStatuses = [404] } = live.current.opts
    let land: () => void
    try {
      const row = await live.current.fetch(key)
      land = () => { live.current.onLoad(row); setNotFound(false); setLoadError('') }
    } catch (err) {
      land = err instanceof ApiError && notFoundStatuses.includes(err.status)
        ? () => { setNotFound(true); setLoadError('') }
        : () => { setNotFound(false); setLoadError(fixedLoadError ? loadFallback : apiDetail(err, loadFallback)) }
    }
    // One synchronous batch with `loading`: a caller whose `onLoad` nulls the id (`GroupEditPage`) strands nothing.
    if (mine !== gen.current) return
    try { land() } catch { setLoadError(live.current.opts.loadFallback) } finally { setLoading(false) }
  }, [])

  useEffect(() => {
    if (id === null) { setLoading(false); return }
    setLoading(true)
    void load(id)
    return () => { gen.current++ }
  }, [id, load])

  const reload = useCallback(async () => { if (id !== null) await load(id) }, [id, load])

  async function confirmAndDelete(options: ConfirmOptions, remove: () => Promise<unknown>) {
    if (!(await confirm(options))) return
    setDeleting(true)
    setDeleteError('')
    try {
      await remove()
      navigate(opts.listTo ?? '', { replace: true })
    } catch (err) {
      setDeleteError(apiDetail(err, opts.deleteFallback ?? ''))
    } finally {
      setDeleting(false)
    }
  }

  return { confirm, confirmDialog, loading, notFound, loadError, reload, deleting, deleteError, confirmAndDelete }
}
