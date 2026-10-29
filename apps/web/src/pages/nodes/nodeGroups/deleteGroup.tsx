import Typography from '@mui/material/Typography'
import { apiDetail } from '../../../api'
import type { ConfirmOptions } from '../../../components/ConfirmDialog'
import type { NodeGroupRow } from '../../../types'
import { monoSx } from '../../../ui/listStyles'
import { deleteNodeGroup } from './api'
import { deleteBlockedReason, deleteQuestion } from './groupFacts'

/** A group's Delete, for its row and its page. Clears `setError` at START, so a cancelled confirm clears
 *  it too. `busy` wraps the write in the host's busy state; `onDeleted` refreshes or leaves. */
export async function deleteGroup(row: NodeGroupRow, { confirm, setError, busy, onDeleted }: {
  confirm: (options: ConfirmOptions) => Promise<boolean>
  setError: (message: string) => void
  busy: (work: () => Promise<void>) => Promise<void>
  onDeleted: () => Promise<unknown>
}): Promise<void> {
  setError('')
  /* A DELETE THAT CANNOT SUCCEED NEVER REACHES A CONFIRM: an ANSWER in the banner, not a confirm whose
     red button offers to ignore the warning (`groupFacts.ts::deleteBlockedReason`). */
  const blocked = deleteBlockedReason(row)
  if (blocked) {
    setError(blocked)
    return
  }
  const ok = await confirm({
    title: `Delete the node group ${row.name}?`,
    /* MONO: the name is read as a TOKEN to match against the intended row (as `Nodes.tsx`'s confirm). */
    identity: <Typography component="span" sx={monoSx}>{row.name}</Typography>,
    message: deleteQuestion(row),
    confirmLabel: 'Delete group',
    destructive: true,
  })
  if (!ok) return
  await busy(async () => {
    try {
      await deleteNodeGroup(row.id)
      await onDeleted()
    } catch (err) {
      setError(apiDetail(err, `Failed to delete ${row.name}.`))
    }
  })
}
