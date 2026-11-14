/* "DONE, NOT YET SEEN" ENDS WHEN ITS VIEWER OPENS THE TASK (`PipelineTask.done_unseen`).
 * The task page reports the open (`POST /api/tasks/{id}/seen`); once it lands, a board still on screen (the split's
 * list beside the page) drops the row's halo at once — the mark is per viewer, so no console event re-reads it.
 * Storage events reach only other tabs, hence a window event of our own (`TASK_SEEN`). */
import { post } from '../api'

/** The window event `markTaskSeen` fires once the server holds the open, its `detail` the task id. */
export const TASK_SEEN = 'jaah:task-seen'

export async function markTaskSeen(id: number): Promise<void> {
  await post<void>(`/api/tasks/${id}/seen`)
  window.dispatchEvent(new CustomEvent<number>(TASK_SEEN, { detail: id }))
}
