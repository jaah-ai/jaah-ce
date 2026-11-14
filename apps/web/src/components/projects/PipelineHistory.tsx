import type { JSX } from 'react'
import { useInfiniteQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { apiDetail } from '../../api'
import { useSubmit } from '../../hooks/useSubmit'
import { getPipelineHistory, type Pipeline, pipelineHistoryKey, type PipelineRevision, restorePipeline } from '../../lib/pipeline'
import { FONT_SIZE, FONT_WEIGHT } from '../../theme'
import Banner from '../../ui/Banner'
import { DisabledReason } from '../../ui/DisabledReason'
import { DetailSection } from '../../ui/detail/DetailPage'
import { tapTargetSx } from '../../ui/listStyles'
import { NoneMark } from '../../ui/NoneMark'
import { RelativeTime } from '../../ui/RelativeTime'
import { useConfirm } from '../ConfirmDialog'

/* PROJECT SETTINGS › PIPELINE › HISTORY (`lib/pipeline.ts`, the API `releases/history.py`): the last saves of the
 * stages, steps and freezes, newest first — who, when, and what each changed — with Restore on every save but the
 * newest (which is what the page shows). Restore brings back the setup that save left, as one new save. */

const ROW_SX = { '& + &': { borderTop: '1px solid', borderColor: 'divider', pt: 1.5 } } as const

export default function PipelineHistory({ projectId, canEdit, dirty, onRestored }: {
  projectId: number
  canEdit: boolean
  /** The editor holds unsaved changes: Restore would drop them, so it waits. */
  dirty: boolean
  onRestored: (fresh: Pipeline) => void
}): JSX.Element {
  const query = useInfiniteQuery({
    queryKey: pipelineHistoryKey(projectId),
    queryFn: ({ pageParam }) => getPipelineHistory(projectId, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: last => last.next_cursor,
  })
  const [confirm, confirmDialog] = useConfirm()
  const { busy, formError, run } = useSubmit()
  const rows = query.data?.pages.flatMap(page => page.items) ?? []

  const restore = (row: PipelineRevision) => void run('Could not restore the pipeline', async () => {
    const ok = await confirm({
      title: 'Restore this save?',
      message: 'Stages, steps and freezes return to how this save left them.',
      confirmLabel: 'Restore',
    })
    if (!ok) return
    onRestored(await restorePipeline(projectId, row.id))
  })

  return (
    <DetailSection title="History" divided testId="pipeline-history">
      {query.isError && <Banner severity="error">{apiDetail(query.error, 'Could not load the history')}</Banner>}
      {formError && <Banner severity="error" testId="pipeline-history-error">{formError}</Banner>}
      {query.isSuccess && rows.length === 0 && <NoneMark />}
      <Stack spacing={1.5}>
        {rows.map((row, index) => (
          <Box key={row.id} sx={ROW_SX} data-testid="pipeline-revision">
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
              <Typography variant="body2" sx={{ fontWeight: FONT_WEIGHT.semibold }}>
                {row.created_by?.name ?? 'System'}
              </Typography>
              <RelativeTime iso={row.created_at} variant="body2" sx={{ color: 'text.secondary' }} />
              {row.kind === 'restore' && (
                <Typography variant="body2" sx={{ color: 'text.secondary' }}>· Restored</Typography>
              )}
              {canEdit && index > 0 && (
                <Box sx={{ ml: 'auto' }}>
                  <DisabledReason reason={dirty ? 'Save or discard your changes first.' : undefined}>
                    <Button size="small" disabled={busy || dirty} onClick={() => restore(row)} sx={tapTargetSx}
                      data-testid="pipeline-revision-restore">
                      Restore
                    </Button>
                  </DisabledReason>
                </Box>
              )}
            </Box>
            <Box component="ul" sx={{ m: 0, mt: 0.5, pl: 2.5, fontSize: FONT_SIZE.sm }} data-testid="pipeline-revision-changes">
              {row.changes.map(line => <li key={line}>{line}</li>)}
            </Box>
          </Box>
        ))}
      </Stack>
      {query.hasNextPage && (
        <Button size="small" onClick={() => void query.fetchNextPage()} disabled={query.isFetchingNextPage}
          sx={[tapTargetSx, { alignSelf: 'flex-start', mt: 1 }]} data-testid="pipeline-history-more">
          Show more
        </Button>
      )}
      {confirmDialog}
    </DetailSection>
  )
}
