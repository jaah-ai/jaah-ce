import type { ReactNode } from 'react'
import { useApiRead } from '../../hooks/useApiRead'
import { useSubmit } from '../../hooks/useSubmit'
import { useConfirm } from '../ConfirmDialog'
import { removeBinding, searchPrincipals, type AccessScope } from './api'

/* Whether the viewer may manage `scope`'s bindings, probed with the principal search (200 → may manage;
   403, or anything else, → read-only): the same gate the Add dialog's picker sits behind. `null` (no scope
   known yet) is read-only. */
export function useCanManage(scope: AccessScope | null): boolean {
  const type = scope?.type
  const id = scope?.id
  // Only this key's own success counts: a held previous answer (`loading`) or a failed probe (`error`) reads false.
  const probe = useApiRead(['access-can-manage', type, id], () => searchPrincipals({ type: type!, id: id! }, '').then(() => true), '',
    { enabled: type !== undefined && id !== undefined })
  return probe.data === true && !probe.loading && !probe.error
}

/* The Remove flow a bindings list runs: confirm (the caller words `message`), delete, re-read on success;
   a failure is `error`, the caller's banner to show. `confirmDialog` must be rendered by the caller. */
export function useRemoveBinding(reload: () => Promise<void>, headers?: HeadersInit): {
  remove: (bindingId: string, message: string) => Promise<void>
  error: string
  clearError: () => void
  confirmDialog: ReactNode
} {
  const { formError: error, setFormError, run } = useSubmit()
  const [confirm, confirmDialog] = useConfirm()
  async function remove(bindingId: string, message: string) {
    const ok = await confirm({ title: 'Remove access', message, confirmLabel: 'Remove', destructive: true })
    if (!ok) return
    let removed = false
    await run('Could not remove access.', async () => {
      await (headers ? removeBinding(bindingId, headers) : removeBinding(bindingId))
      removed = true
    })
    // After the write, not inside it: a failed re-read must not read as "could not remove".
    if (removed) await reload()
  }
  return { remove, error, clearError: () => setFormError(''), confirmDialog }
}
