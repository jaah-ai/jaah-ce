import { useCallback, useEffect } from 'react'
import { useConfirm } from '../components/ConfirmDialog'
import { useApiRead } from './useApiRead'
import { useRowActions } from './useBusyIds'

/* The controller of a Settings CRUD panel that owns its own fetch and writes (`settings/tags/TagsSection`
 * says why): the rows, the load / row-write failure banners, per-row busy flags and the confirm dialog.
 *
 * The read is `useApiRead`'s: ONE REFETCH, MANY CALLERS (save, every row write), and react-query lands only
 * the newest answer (an overtaken one, or one landing after unmount, is dropped).
 * SHAPE-CHECKED when `shapeError` is given: `get<T>` is an unchecked cast, and these panels render as the
 * fallback when the rest of `/settings` fails — a non-list is a load failure, never a white screen.
 * `fetch` returns the rows plus `extra` for whatever else the same response carries: both land together as
 * one answer, so a remount's cached paint carries both (a setter closure in the cache would be a dead
 * mount's), and `extra` is `undefined` beside a malformed answer. */
export interface SettingsRowsOptions<X> {
  fetch: () => Promise<{ rows: unknown; extra?: X }>
  /** The load banner's fallback when the request fails. */
  failed: string
  /** The load banner when the rows are not a list; omitted = unchecked. */
  shapeError?: string
  /** A failed FIRST load still settles the rows to `[]` (ends the loading state). */
  emptyOnFailure?: boolean
  /** False = no request at all (a reader refused the read). */
  enabled?: boolean
}

export function useSettingsRows<T, X = undefined>({
  fetch, failed, shapeError, emptyOnFailure, enabled = true,
}: SettingsRowsOptions<X>) {
  const { busy: busyIds, error: rowError, setError: setRowError, run } = useRowActions<string>()
  const [confirm, confirmDialog] = useConfirm()
  const { data, error, refresh } = useApiRead(
    /* Keyed on the banner text: each panel's load-failure banner names its own rows, so it is the one string
       every caller already passes that is unique per panel — no second name to keep in step with it. Two panels
       with the same text would share one cache entry; today the four texts differ, and a copied text would show
       as the wrong banner before it showed as the wrong rows. */
    ['settings-rows', failed],
    async () => {
      const { rows, extra } = await fetch()
      /* The hook's own check, not `useApiRead`'s `shapeError`: that one throws, and `apiDetail` composes the banner
         as "<failed> — <shapeError>", naming the rows twice. Here the sentence stands alone and the rows settle
         to `[]` (the panel draws empty under the banner, never the skeleton), as the hand-rolled read did. */
      if (shapeError !== undefined && !Array.isArray(rows)) return { rows: [] as T[], extra: undefined, shapeError }
      return { rows: rows as T[], extra, shapeError: undefined }
    },
    failed,
    { enabled },
  )

  /* A refused reader sees nothing of a cached answer: react-query hands cached data back under `enabled: false`,
     and a reader who lost the key mid-session must not keep the earlier rows. */
  const answer = enabled ? data : null
  /* A request failure is always the LATEST outcome (react-query clears it on the next success); a held malformed
     answer speaks only when no request failed after it. */
  const loadError = enabled ? error || (answer?.shapeError ?? '') : ''

  /* Once react-query has settled on a well-shaped answer (one overtaken by a later refetch never gets here): a
     stale row failure cleared, or it sits over the successful write that followed. */
  useEffect(() => {
    if (!answer || answer.shapeError) return
    setRowError('')
  }, [answer, setRowError])

  /* `null` = nothing landed yet (the skeleton). A failed FIRST load settles to `[]` only when asked; a later
     failure keeps the last rows (`useApiRead` holds them under the banner). */
  const rows = answer?.rows ?? (loadError && emptyOnFailure ? [] : null)

  // Guarded here too, not only at mount: a refused reader's cards still call it, and `refetch` ignores `enabled`.
  const load = useCallback(async () => { if (enabled) await refresh() }, [enabled, refresh])

  /** One row write: busy while in flight, refetch after, the refusal over the list. */
  async function rowWrite(busyKey: string, fallback: string, act: () => Promise<unknown>) {
    await run(busyKey, fallback, async () => {
      await act()
      await load()
    })
  }

  return {
    rows, extra: answer?.extra, loadError, rowError, setRowError, load, busyIds, rowWrite, confirm, confirmDialog,
  }
}
