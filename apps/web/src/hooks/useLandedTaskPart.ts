import { useEffect } from 'react'
import { registerLandedTaskPart, type TaskPagePart } from '../lib/taskPageSeed'

/** A task-page panel's answer, offered to its remount across the split width
 *  (`lib/taskPageSeed.ts::registerLandedTaskPart`). `value` undefined = nothing to offer (loading, failed);
 *  `busy.current` true = a re-read is in flight or pending, read when the remount asks. */
export function useLandedTaskPart(
  taskId: number | null | undefined,
  part: TaskPagePart,
  value: unknown,
  busy?: { readonly current: boolean },
): void {
  useEffect(() => {
    if (taskId == null || value === undefined) return
    return registerLandedTaskPart(taskId, part, value, busy && (() => busy.current))
  }, [taskId, part, value, busy])
}
