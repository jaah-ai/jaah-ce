import { useCallback, useMemo, useRef } from 'react'
import { get } from '../../api'
import { useApiRead } from '../../hooks/useApiRead'
import { useEventStream, type ConsoleEvent } from '../../hooks/useEventStream'
import { useLandedTaskPart } from '../../hooks/useLandedTaskPart'
import { useLiveRefetch } from '../../hooks/useLiveRefetch'
import { absoluteTime } from '../../lib/relativeTime'
import { takeTaskPageSeed } from '../../lib/taskPageSeed'
import type { TaskControlMessage } from '../../types'
import type { ActivityLogEntry } from './ActivityLog'

// The control-plane notices typed into this task's runner pane (`GET /api/tasks/{id}/control-messages`), oldest
// first, as Details › Timeline rows: a troubleshooting fact, so a one-liner there and nowhere else. The endpoint also
// lists the operator's delivered answers (`kind: 'answer'`); those are the Q&A tab's, so they are dropped here.
//
// Live: a notice's mint and its delivery each emit the QUIET `control_message` action on this task
// (`hooks/useLiveRefetch.ts`), the one event this read listens for.

const LOAD_ERROR = 'Failed to load pane messages'

function delivery(m: TaskControlMessage): string {
  if (m.delivered) return m.delivered_at ? `Delivered ${absoluteTime(m.delivered_at)}` : 'Delivered'
  return m.dropped ? 'Dropped' : 'Pending'
}

/** The control-plane notices among the pane messages: the answers are the Q&A tab's. */
export function noticesOf(messages: TaskControlMessage[] | null | undefined): TaskControlMessage[] {
  return (messages ?? []).filter(m => m.kind === 'message')
}

/** The notices as Timeline rows (`ActivityLog`'s `control_message` note), merged into `entries` by time. A notice from
 * before the task's last Reset belongs to the abandoned attempt, so it folds with that attempt's rows. */
export function withNotices(entries: ActivityLogEntry[], messages: TaskControlMessage[] | null | undefined, taskId: number): ActivityLogEntry[] {
  const notices = noticesOf(messages)
  if (notices.length === 0) return entries
  const resets = entries.filter(e => e.category === 'transition' && e.action === 'reset' && Number(e.data.id) === taskId)
  const lastReset = resets.length > 0 ? Date.parse(resets[resets.length - 1].created_at) : null
  const rows: ActivityLogEntry[] = notices.map(m => ({
    category: 'note',
    action: 'control_message',
    label: 'Control-plane notice',
    summary: m.text,
    state: null,
    created_at: m.created_at,
    duration_seconds: null,
    actor: null,
    ...(lastReset !== null && Date.parse(m.created_at) < lastReset ? { earlier_attempt: true } : {}),
    data: { id: taskId, sender: m.sender, delivery: delivery(m) },
  }))
  // Stable: a notice lands after a feed row stamped at the same instant.
  return [...entries, ...rows]
    .map((e, i) => ({ e, i, t: Date.parse(e.created_at) }))
    .sort((x, y) => x.t - y.t || x.i - y.i)
    .map(x => x.e)
}

/** The task's pane messages, live. Read once by the page (`useTaskDetailTabs`), which merges the notices into the
 * Timeline (whether that subtab exists depends on them); `null` (a create) reads nothing. */
export function useTaskNotices(taskId: number | null) {
  const { data, error, loading, refresh } = useApiRead(
    ['task-control-messages', taskId],
    async () => {
      const rows = takeTaskPageSeed<TaskControlMessage[]>(taskId as number, 'control_messages')
        ?? await get<TaskControlMessage[]>(`/api/tasks/${taskId}/control-messages`)
      return Array.isArray(rows) ? rows : []
    },
    LOAD_ERROR,
    { enabled: taskId !== null },
  )

  /* An own-task event leaves the list stale from the moment it is heard (the re-read is debounced): not offered to a
     remount across the split width until that re-read settles. */
  const heard = useRef(0)
  const settled = useRef(0)
  const stale = useMemo(() => ({ get current() { return heard.current > settled.current } }), [])
  const ownTask = useCallback((event: ConsoleEvent) => {
    const own = taskId !== null && Number(event.id) === taskId
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
  useLiveRefetch(stream, reload, null, { entity: 'task', actions: ['control_message'], match: ownTask })
  /* None while a task's first answer loads (a key change keeps the last task's rows meanwhile), so the Timeline never
     shows another task's notices. */
  return loading || error || !data ? null : data
}
