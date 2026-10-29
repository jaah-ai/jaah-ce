import type { ReactNode } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { apiDetail, del } from '../../api'
import { TASK_TREE_QUERY_KEY } from '../../hooks/useTaskTree'
import { useConfirm } from '../ConfirmDialog'
import { PANE_QUERY_KEY } from './pane/queryKeys'

/* DELETE A FEATURE — the one confirm + `DELETE /api/features/{id}` behind every Delete entry point: the
 * feature header's "…" menu (`pane/FeatureHeader.tsx`), the rail's row menu (`rail/TaskTree.tsx`) and the
 * Edit page (`pages/FeatureEditPage.tsx`). Its tasks are kept: the server leaves them under no feature. The
 * caller renders `confirmDialog` and decides where to go after (`onDeleted`, with the feature's project). */

export type DeletableFeature = { id: string; name: string; project_id: number }

export function useDeleteFeature({ onDeleted, onError }: {
  onDeleted: (projectId: number, featureId: string) => void
  onError: (message: string) => void
}): { deleteFeature: (feature: DeletableFeature) => Promise<void>; busy: boolean; confirmDialog: ReactNode } {
  const queryClient = useQueryClient()
  const [confirm, confirmDialog] = useConfirm()
  const remove = useMutation({
    mutationFn: (feature: DeletableFeature) => del(`/api/features/${encodeURIComponent(feature.id)}`),
    onSuccess: (_, feature) => {
      void queryClient.invalidateQueries({ queryKey: TASK_TREE_QUERY_KEY })
      void queryClient.invalidateQueries({ queryKey: PANE_QUERY_KEY })
      onDeleted(feature.project_id, feature.id)
    },
    onError: err => onError(apiDetail(err, 'Could not delete.')),
  })

  const deleteFeature = async (feature: DeletableFeature) => {
    const confirmed = await confirm({
      title: `Delete “${feature.name}”?`,
      message: 'Its tasks move to “Other”. No task is deleted or changed.',
      confirmLabel: 'Delete',
      destructive: true,
    })
    if (confirmed) remove.mutate(feature)
  }

  return { deleteFeature, busy: remove.isPending, confirmDialog }
}
