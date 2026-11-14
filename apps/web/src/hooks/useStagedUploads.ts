/* Files staged on a composer before the thing they ride exists. Each uploads AT ONCE (`upload`), so Send
 * only has ids to hand over. One instance per draft.
 * ── Refusals ── `refuse` decides before any round trip; its line and an upload's failure go through
 * `onError`, never thrown.
 * ── Lifetime ── A local `blob:` preview is revoked when its row leaves (`remove`/`prune`/`clear`) and on
 * unmount. An upload that lands after its row left is dropped; after `clear`, its error is too.
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import { apiDetail } from '../api'

export type StagedUpload = {
  key: string
  filename: string
  contentType: string
  /** A `blob:` URL for a fresh pick with a preview; a caller's own URL on a row it seeded; else null. */
  src: string | null
  /** Null until the upload answers. */
  id: number | null
  status: 'uploading' | 'ready' | 'failed'
  error?: string
}

type Options = {
  /** Why this file is not staged, given how many are; `stop` refuses the rest of the pick with it. */
  refuse: (file: File, staged: number) => { message: string; stop?: boolean } | null
  /** Which files get a local preview; all of them when absent. */
  preview?: (file: File) => boolean
  /** Null: the upload no longer belongs to this draft, so its row is left alone. */
  upload: (file: File) => Promise<{ id: number } | null>
  /** The failure line for an upload the server gave no reason for. */
  failure: (filename: string) => string
  onError: (message: string) => void
}

export function releaseStaged(rows: readonly StagedUpload[]): void {
  for (const row of rows) if (row.src?.startsWith('blob:')) URL.revokeObjectURL(row.src)
}

export function useStagedUploads(options: Options) {
  const [files, setFiles] = useState<StagedUpload[]>([])
  const nextKey = useRef(0)
  const epoch = useRef(0)
  const opts = useRef(options)
  opts.current = options
  /* The rows as of the last render plus any staged since: the count cap and the unmount revoke both
     need them outside a state updater. */
  const latest = useRef(files)
  latest.current = files

  useEffect(() => () => releaseStaged(latest.current), [])

  /** Puts these rows on the draft as they are; the ones they replace are NOT released. */
  const replace = useCallback((rows: StagedUpload[]) => {
    latest.current = rows
    setFiles(rows)
  }, [])

  const add = useCallback((picked: readonly File[]) => {
    const mine = epoch.current
    for (const file of picked) {
      const refused = opts.current.refuse(file, latest.current.length)
      if (refused) {
        opts.current.onError(refused.message)
        if (refused.stop) break
        continue
      }
      const key = `new-${++nextKey.current}`
      const contentType = file.type || 'application/octet-stream'
      // The clipboard names every pasted image the same.
      const filename = file.name || 'image.png'
      const src = opts.current.preview?.(file) === false ? null : URL.createObjectURL(file)
      const staged: StagedUpload = { key, filename, contentType, src, id: null, status: 'uploading' }
      latest.current = [...latest.current, staged]
      setFiles(current => [...current, staged])
      const settle = (patch: Partial<StagedUpload>) => {
        if (mine === epoch.current) setFiles(current => current.map(f => (f.key === key ? { ...f, ...patch } : f)))
      }
      opts.current.upload(file).then(
        a => { if (a) settle({ id: a.id, status: 'ready' }) },
        err => {
          const error = apiDetail(err, opts.current.failure(filename))
          settle({ status: 'failed', error })
          if (mine === epoch.current) opts.current.onError(error)
        },
      )
    }
  }, [])

  /** Keeps the rows `keep` passes and releases the rest. */
  const prune = useCallback((keep: (row: StagedUpload) => boolean) => {
    setFiles(current => {
      releaseStaged(current.filter(row => !keep(row)))
      return current.filter(keep)
    })
  }, [])

  const remove = useCallback((key: string) => prune(row => row.key !== key), [prune])

  /** Empties the draft and disowns every upload still in flight. */
  const clear = useCallback(() => {
    epoch.current += 1
    releaseStaged(latest.current)
    replace([])
  }, [replace])

  return { files, uploading: files.some(f => f.status === 'uploading'), add, remove, prune, replace, clear }
}
