import { useCallback, useMemo } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useAuth } from '../auth'
import { PANE_QUERY_KEY } from '../components/tasks/pane/queryKeys'
import { readPinned, TASK_PINS_SAVED, toggledPinned } from '../lib/taskPin'
import type { PipelineTask } from '../types'

/** This user's task pins (`lib/taskPin.ts`) as the row handlers' pair: `isPinned` and `onTogglePin`. A toggle saves
 *  the preference (optimistic, `auth.tsx::updatePreferences`); once saved it re-reads the counts and tells the board
 *  (`TASK_PINS_SAVED`) to re-read its rows, whose page the server orders pins-first. `isPinned` keeps its identity
 *  while the pins do not change, so an unrelated preference save does not rebuild every row. */
export function useTaskPins(): {
  isPinned: (task: Pick<PipelineTask, 'id'>) => boolean
  onTogglePin: (task: Pick<PipelineTask, 'id'>) => void
} {
  const { admin, updatePreferences } = useAuth()
  const queryClient = useQueryClient()
  const pinnedKey = readPinned(admin?.preferences).join(' ')
  const isPinned = useMemo(() => {
    const ids = new Set(pinnedKey === '' ? [] : pinnedKey.split(' ').map(Number))
    return (task: Pick<PipelineTask, 'id'>) => ids.has(task.id)
  }, [pinnedKey])
  const onTogglePin = useCallback((task: Pick<PipelineTask, 'id'>) => {
    void updatePreferences(prefs => ({ task_pinned: toggledPinned(readPinned(prefs), task.id) })).then(saved => {
      if (!saved) return
      void queryClient.invalidateQueries({ queryKey: PANE_QUERY_KEY })
      window.dispatchEvent(new Event(TASK_PINS_SAVED))
    })
  }, [updatePreferences, queryClient])
  return { isPinned, onTogglePin }
}
