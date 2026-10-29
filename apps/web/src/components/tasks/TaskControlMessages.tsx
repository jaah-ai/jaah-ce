import { useCallback, useEffect, useMemo, useRef } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { get } from '../../api'
import { useApiRead } from '../../hooks/useApiRead'
import { useEventStream, type ConsoleEvent } from '../../hooks/useEventStream'
import { useLiveRefetch } from '../../hooks/useLiveRefetch'
import { clockTime, fmtTimestamp } from '../../lib/relativeTime'
import { takeTaskPageSeed } from '../../lib/taskPageSeed'
import { useLandedTaskPart } from '../../hooks/useLandedTaskPart'
import { Banner } from '../../ui/Banner'
import { StatusChip } from '../../ui/StatusChip'
import type { TaskControlMessage } from '../../types'

// What reached this task's runner pane from outside it (`GET /api/tasks/{id}/control-messages`): control-plane
// notices and operator answers, oldest first. Each row is `#control-message-<id>`, so the header chip
// (`ControlMessagesChip`) and a link can land on one. The kind marker matches the console overlay
// (`components/consoleLabels.ts`): `control plane` in the warning tone, `operator` in the info tone; anything
// unmarked in the console is the runner's own.
//
// Live: a mint or a delivery emits the QUIET `control_message` action on this task (`hooks/useLiveRefetch.ts`);
// an answer's delivery also moves the task's state, so this task's ordinary events re-read too.

const LOAD_ERROR = 'Failed to load pane messages'

const KIND_MARKER = {
  message: { label: 'control plane', tone: 'warning' },
  answer: { label: 'operator', tone: 'info' },
} as const

function deliveryChip(m: TaskControlMessage) {
  if (m.delivered) {
    return <StatusChip label="Delivered" tone="success" title={m.delivered_at ? fmtTimestamp(m.delivered_at) : undefined} />
  }
  if (m.dropped) return <StatusChip label="Dropped" tone="neutral" title="Retired undelivered" />
  return <StatusChip label="Pending" tone="neutral" />
}

export function TaskControlMessages({
  taskId,
  onCountChange,
}: {
  taskId: number
  /** Reports the count upward, so the page's header chip needs no second request. */
  onCountChange?: (count: number) => void
}): JSX.Element | null {
  const { data, error, loading, refresh } = useApiRead(
    ['task-control-messages', taskId],
    async () => {
      const rows = takeTaskPageSeed<TaskControlMessage[]>(taskId, 'control_messages')
        ?? await get<TaskControlMessage[]>(`/api/tasks/${taskId}/control-messages`)
      return Array.isArray(rows) ? rows : []
    },
    LOAD_ERROR,
  )

  useEffect(() => {
    if (data) onCountChange?.(data.length)
  }, [data, onCountChange])

  /* An own-task event leaves the list stale from the moment it is heard (the re-read is debounced): not offered to a
     remount across the split width until that re-read settles. */
  const heard = useRef(0)
  const settled = useRef(0)
  const stale = useMemo(() => ({ get current() { return heard.current > settled.current } }), [])
  const ownTask = useCallback((event: ConsoleEvent) => {
    const own = Number(event.id) === taskId
    if (own) heard.current += 1
    return own
  }, [taskId])
  // Settles only the events heard before this re-read began: one heard during it stays stale.
  const reload = useCallback(() => {
    const upTo = heard.current
    void refresh().finally(() => { settled.current = Math.max(settled.current, upTo) })
  }, [refresh])
  useLandedTaskPart(taskId, 'control_messages', loading || error || !data ? undefined : data, stale)
  const stream = useEventStream()
  useLiveRefetch(stream, reload, null, { entity: 'task', match: ownTask })
  useLiveRefetch(stream, reload, null, { entity: 'task', actions: ['control_message'], match: ownTask })

  if (error) return <Banner severity="error">{LOAD_ERROR}</Banner>
  if (!data || data.length === 0) return null
  return (
    <Box component="ol" data-testid="task-control-messages" sx={{ listStyle: 'none', m: 0, p: 0, display: 'grid', gap: 1 }}>
      {data.map(m => {
        const marker = KIND_MARKER[m.kind] ?? KIND_MARKER.message
        return (
          <Box
            component="li"
            key={m.id}
            id={`control-message-${m.id}`}
            data-kind={m.kind}
            sx={{
              display: 'grid',
              gap: 0.5,
              pl: 1,
              borderLeft: 2,
              borderColor: `${marker.tone}.main`,
              scrollMarginTop: 80,
            }}
          >
            <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 1, minWidth: 0 }}>
              <Typography
                component="time"
                dateTime={m.created_at}
                title={fmtTimestamp(m.created_at)}
                variant="caption"
                sx={{ color: 'text.secondary', fontVariantNumeric: 'tabular-nums' }}
              >
                {clockTime(m.created_at)}
              </Typography>
              <StatusChip label={marker.label} tone={marker.tone} />
              <Typography variant="body2" sx={{ fontWeight: 600, minWidth: 0, overflowWrap: 'anywhere' }}>
                {m.sender}
              </Typography>
              <Box sx={{ ml: 'auto' }}>{deliveryChip(m)}</Box>
            </Box>
            <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
              {m.text}
            </Typography>
          </Box>
        )
      })}
    </Box>
  )
}

/** The page header's jump to the list: `✉ <count>`, nothing at zero. */
export function ControlMessagesChip({ count, onClick }: { count: number; onClick: () => void }): JSX.Element | null {
  if (count <= 0) return null
  return (
    <StatusChip
      label={String(count)}
      glyph="✉"
      tone="neutral"
      ariaLabel="Control-plane messages"
      title={`${count} pane message${count === 1 ? '' : 's'}`}
      onClick={onClick}
      press={{ glyph: '↓', label: 'Messages' }}
    />
  )
}
