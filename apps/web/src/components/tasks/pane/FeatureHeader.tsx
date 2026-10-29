import { useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import IconButton from '@mui/material/IconButton'
import Skeleton from '@mui/material/Skeleton'
import Typography from '@mui/material/Typography'
import type { Theme } from '@mui/material/styles'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { apiDetail, get, post } from '../../../api'
import { TASK_TREE_QUERY_KEY } from '../../../hooks/useTaskTree'
import type { FeatureOut, TaskTreeCounts } from '../../../types'
import FeatureIcon from '../FeatureIcon'
import { parseFeatureIcon } from '../../../lib/featureIcon'
import { PHONE } from '../../../ui/listStyles'
import { PANE_QUERY_KEY, featureQueryKey } from './queryKeys'
import SegmentedBar from './SegmentedBar'
import { VISUALLY_HIDDEN } from '../../../ui/visuallyHidden'
import { useConfirm } from '../../ConfirmDialog'
import { shortDay } from '../../../lib/relativeTime'
import StatusChip from '../../../ui/StatusChip'
import { OverflowMenu } from '../../../ui/OverflowMenu'
import { useDeleteFeature } from '../useDeleteFeature'

/* THE FEATURE HEADER — the block above the task list when the selection is one feature: its icon, name,
 * description (read-only) and progress, and the verbs on it (ship/reopen, delete). Nothing is edited
 * inline: the one pencil opens the Feature Edit page (`pages/FeatureEditPage.tsx`, `/features/:id`), which
 * also owns Owner and Created. The progress
 * reads the SAME tree `counts` the rail and the container rows draw, so the three agree. Nothing ships by
 * itself: "All done" is a prompt, the operator still presses the button. */

export type FeatureHeaderProps = {
  featureId: string
  /** The feature's Stopped/In progress/Done counts from the task tree, or undefined while loading. */
  counts: TaskTreeCounts | undefined
  /** Called after a successful delete with the feature's project id. */
  onDeleted: (projectId: number) => void
  /** Its name from the task tree, for the page heading while the feature itself loads or fails to. */
  treeName?: string
}

// The touch floor at phone width, from the theme's single home (never a hardcoded 44).
const TAP_SX = (theme: Theme) => ({ [PHONE]: { minHeight: theme.custom.tapTargetPx, minWidth: theme.custom.tapTargetPx } })

export default function FeatureHeader({ featureId, counts, onDeleted, treeName }: FeatureHeaderProps) {
  const queryClient = useQueryClient()
  const { data: feature, isError } = useQuery({
    queryKey: featureQueryKey(featureId),
    queryFn: () => get<FeatureOut>(`/api/features/${featureId}`),
  })
  const [confirm, confirmDialog] = useConfirm()
  const [error, setError] = useState<string | null>(null)

  // Every write moves the tree's counts or a pane row, so each invalidates both.
  function settled(updated?: FeatureOut) {
    if (updated) queryClient.setQueryData(featureQueryKey(featureId), updated)
    void queryClient.invalidateQueries({ queryKey: TASK_TREE_QUERY_KEY })
    void queryClient.invalidateQueries({ queryKey: PANE_QUERY_KEY })
    setError(null)
  }

  const transition = useMutation({
    mutationFn: (verb: 'ship' | 'reopen') => post<FeatureOut>(`/api/features/${featureId}/${verb}`),
    onSuccess: updated => settled(updated),
    onError: (err, verb) => setError(apiDetail(err, verb === 'ship' ? 'Could not ship.' : 'Could not reopen.')),
  })
  const remove = useDeleteFeature({ onDeleted: projectId => onDeleted(projectId), onError: setError })

  /* The page's `<h1>` is this header's name (the pane header draws none on a feature), so the two nameless states
     keep a hidden one. */
  const pendingHeading = <Box component="h1" sx={VISUALLY_HIDDEN}>{treeName ?? 'Feature'}</Box>
  if (isError) {
    return (
      <>
        {pendingHeading}
        <Typography variant="caption" color="error" data-testid="feature-header-error" sx={{ display: 'block', px: 2, py: 1 }}>
          Could not load this feature.
        </Typography>
      </>
    )
  }
  if (!feature) {
    return (
      <>
        {pendingHeading}
        <Skeleton variant="rounded" height={56} sx={{ mx: 2, my: 1 }} data-testid="feature-header-loading" />
      </>
    )
  }

  const canEdit = feature.can_edit
  const active = feature.status === 'active'
  const open = counts ? counts.stopped + counts.in_progress + (counts.draft ?? 0) : 0
  const allDone = !!counts && counts.total > 0 && counts.done === counts.total
  const busy = transition.isPending || remove.busy

  const ship = async () => {
    // An open task is the one thing worth a second look; a completed feature ships in one press.
    const confirmed =
      open === 0 ||
      (await confirm({
        title: `Ship “${feature.name}”?`,
        message: counts?.draft
          ? `${counts.stopped} Stopped, ${counts.in_progress} In progress and ${counts.draft} Draft tasks stay as they are.`
          : `${counts?.stopped ?? 0} Stopped and ${counts?.in_progress ?? 0} In progress tasks stay as they are.`,
        confirmLabel: 'Mark shipped',
      }))
    if (confirmed) transition.mutate('ship')
  }

  return (
    <Box
      data-testid="feature-header"
      sx={{ px: 2, py: 1.5, display: 'flex', flexDirection: 'column', gap: 0.75, minWidth: 0, borderBottom: 1, borderColor: 'divider' }}
    >
      <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 1, minWidth: 0 }}>
        <Box sx={{ flex: '1 1 auto', minWidth: 0, display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
          <Box data-testid="feature-name" sx={{ flex: '1 1 auto', minWidth: 0, display: 'flex', alignItems: 'flex-start', gap: 0.75 }}>
            {/* No icon renders nothing, so the title starts flush (no placeholder gap). The icon's box is one
                title line tall, so it centres on the FIRST line when a long name wraps (390px). */}
            {parseFeatureIcon(feature.icon) && (
              <Box sx={{ flex: '0 0 auto', height: '1.3em', typography: 'h6', display: 'flex', alignItems: 'center' }}>
                <FeatureIcon icon={feature.icon} testId="feature-header-icon" />
              </Box>
            )}
            <Typography
              variant="h6"
              component="h1"
              title={feature.name}
              sx={{ minWidth: 0, overflowWrap: 'anywhere', lineHeight: 1.3 }}
            >
              {feature.name}
            </Typography>
            {canEdit && (
              <IconButton
                size="small"
                component={RouterLink}
                to={`/features/${encodeURIComponent(feature.id)}`}
                aria-label="Edit feature"
                data-testid="feature-edit"
                sx={TAP_SX}
              >
                <Box component="span" aria-hidden sx={{ fontSize: '0.875rem', lineHeight: 1 }}>✎</Box>
              </IconButton>
            )}
          </Box>
          {!active && <StatusChip label="Shipped" tone="success" />}
        </Box>
        {canEdit && (
          <OverflowMenu
            size="small"
            ariaLabel="Feature actions"
            testId="feature-menu"
            sx={[TAP_SX, { lineHeight: 1 }]}
            items={[{
              key: 'delete', label: 'Delete', ariaLabel: 'Delete', testId: 'feature-delete', danger: true, disabled: busy,
              onClick: () => void remove.deleteFeature(feature),
            }]}
          />
        )}
      </Box>

      {feature.description && (
        <Typography
          variant="body2"
          data-testid="feature-description"
          sx={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}
        >
          {feature.description}
        </Typography>
      )}

      {!active && feature.shipped_at && (
        <Box data-testid="feature-meta" sx={{ typography: 'caption', color: 'text.secondary', minWidth: 0 }}>
          <Box component="span" data-testid="feature-shipped">
            Shipped {shortDay(feature.shipped_at)}
            {feature.shipped_by ? ` by ${feature.shipped_by.name}` : ''}
          </Box>
        </Box>
      )}

      {(counts || canEdit) && (
        <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 1.5, rowGap: 0.75, minWidth: 0 }}>
          {counts && (
            <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 1, rowGap: 0.25, typography: 'body2' }}>
              {/* The done share and its bar only where the feature shows progress (`models.Feature.show_progress`):
                  off, it is a task bucket, and only the breakdown stays. */}
              {feature.show_progress && (
                <>
                  <Box component="span" data-testid="feature-progress" sx={{ fontWeight: 600 }}>
                    {counts.done} of {counts.total} done
                  </Box>
                  <SegmentedBar counts={counts} width={120} testId="feature-bar" />
                </>
              )}
              <Box component="span" sx={{ typography: 'caption', color: 'text.secondary' }}>
                {counts.stopped} stopped · {counts.in_progress} in progress · {counts.draft > 0 && <>{counts.draft} draft · </>}{counts.done} done
              </Box>
            </Box>
          )}
          {canEdit && active && (
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
              {allDone && (
                <Typography variant="body2" data-testid="feature-all-done" sx={{ color: 'success.main', fontWeight: 600 }}>
                  All done — ship it?
                </Typography>
              )}
              <Button
                size="small"
                variant={allDone ? 'contained' : 'outlined'}
                data-testid="feature-ship"
                disabled={busy}
                onClick={() => void ship()}
                sx={TAP_SX}
              >
                Mark shipped
              </Button>
            </Box>
          )}
          {canEdit && !active && (
            <Button
              size="small"
              variant="outlined"
              data-testid="feature-reopen"
              disabled={busy}
              onClick={() => transition.mutate('reopen')}
              sx={TAP_SX}
            >
              Reopen
            </Button>
          )}
        </Box>
      )}

      {error && (
        <Typography variant="caption" color="error" role="alert" data-testid="feature-header-alert">
          {error}
        </Typography>
      )}

      {confirmDialog}
      {remove.confirmDialog}
    </Box>
  )
}
