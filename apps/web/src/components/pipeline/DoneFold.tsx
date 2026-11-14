import { type JSX, type ReactNode, useCallback, useMemo, useState } from 'react'
import Box from '@mui/material/Box'
import { useInfiniteQuery } from '@tanstack/react-query'
import { apiDetail } from '../../api'
import { useLoadMoreSentinel } from '../../hooks/useLoadMoreSentinel'
import { getPipelineItemsPage, pipelineItemsKey, type ItemFilters, type PipelineItem } from '../../lib/pipelineBoard'
import type { PipelineTask } from '../../types'
import type { TaskListFold } from '../tasks/listViews/types'
import { FONT_SIZE } from '../../theme'

/* THE FIRST STAGE'S DONE FOLD — the finished tasks that shipped no code (an answer, research, a design, "no change
 * needed": `membership.py`'s `done` bucket) sit outside every stage, so on their own they would vanish from the board
 * once done. The first stage (Ideas) lists them under one closed "Done N" row; opening it reads the bucket a page at a
 * time (`GET …/pipeline/items?stage=done`) under the list's filters. The rows are the board's own (`render`). */

export type DoneFoldProps = {
  projectId: number
  filters: ItemFilters
  /** The bucket's count from the view (`view.done`). */
  count: number
  /** The fold's feed: its row, and its tasks under it while open (`busy`: the first page is loading). */
  render: (tasks: PipelineTask[], fold: TaskListFold, busy: boolean) => ReactNode
}

export default function DoneFold({ projectId, filters, count, render }: DoneFoldProps): JSX.Element | null {
  const [open, setOpen] = useState(false)
  const read = useInfiniteQuery({
    queryKey: [...pipelineItemsKey(projectId), 'done', null, JSON.stringify(filters)],
    queryFn: ({ signal, pageParam }) =>
      getPipelineItemsPage(projectId, { stage: 'done', group: null }, filters, pageParam, signal),
    initialPageParam: null as string | null,
    enabled: open,
    getNextPageParam: last => last.next ?? undefined,
    // The last answer stays up while a filter's re-read loads, as the stage list's does.
    placeholderData: previous => previous,
    meta: { persist: false },
  })
  const canReadMore = open && read.hasNextPage && !read.isFetchingNextPage && !read.isError && !read.isPlaceholderData
  const sentinelRef = useLoadMoreSentinel(canReadMore, () => read.fetchNextPage())
  const pages = read.data?.pages
  /* One rows array and one fold object while their inputs hold, so the feed keeps its memoized rows. */
  const tasks = useMemo(() => (open ? (pages ?? []) : []).flatMap(page => page.rows).flatMap((item: PipelineItem) =>
    item.task ? [{ ...item.task, pipeline_group: item.group, pipeline_stage: item.stage_id }] : []), [open, pages])
  const onToggle = useCallback(() => setOpen(was => !was), [])
  const fold = useMemo<TaskListFold>(() => ({ label: 'Done', glyph: '✓', open, count, onToggle }), [open, count, onToggle])
  if (count <= 0) return null
  return (
    <Box data-testid="pipeline-done-fold">
      {render(tasks, fold, open && read.isPending)}
      {open && read.isError && (
        <Box role="alert" sx={{ color: 'error.main', fontSize: FONT_SIZE.sm, py: 1 }}>
          {apiDetail(read.error, 'Could not load done tasks')}
        </Box>
      )}
      {canReadMore && <Box ref={sentinelRef} aria-hidden="true" role="presentation" sx={{ height: 1 }} />}
    </Box>
  )
}
