import { useEffect, useState, type ReactNode } from 'react'
import { apiDetail } from '../../api'
import { useConfirm } from '../ConfirmDialog'
import { removeBinding, searchPrincipals, type AccessScope } from './api'

/* Whether the viewer may manage `scope`'s bindings, probed with the principal search (200 → may manage;
   403, or anything else, → read-only): the same gate the Add dialog's picker sits behind. `null` (no scope
   known yet) is read-only. */
export function useCanManage(scope: AccessScope | null): boolean {
  const [canManage, setCanManage] = useState(false)
  const type = scope?.type
  const id = scope?.id
  useEffect(() => {
    setCanManage(false)
    if (type === undefined || id === undefined) return
    let live = true
    searchPrincipals({ type, id }, '').then(() => { if (live) setCanManage(true) }, () => {})
    return () => { live = false }
  }, [type, id])
  return canManage
}

/* The Remove flow a bindings list runs: confirm (the caller words `message`), delete, re-read on success;
   a failure is `error`, the caller's banner to show. `confirmDialog` must be rendered by the caller. */
export function useRemoveBinding(reload: () => Promise<void>, headers?: HeadersInit): {
  remove: (bindingId: string, message: string) => Promise<void>
  error: string
  clearError: () => void
  confirmDialog: ReactNode
} {
  const [error, setError] = useState('')
  const [confirm, confirmDialog] = useConfirm()
  async function remove(bindingId: string, message: string) {
    const ok = await confirm({ title: 'Remove access', message, confirmLabel: 'Remove', destructive: true })
    if (!ok) return
    setError('')
    try {
      await (headers ? removeBinding(bindingId, headers) : removeBinding(bindingId))
    } catch (err) {
      setError(apiDetail(err, 'Could not remove access.'))
      return
    }
    await reload()
  }
  return { remove, error, clearError: () => setError(''), confirmDialog }
}
