/* THE TASK PAGE'S CHUNK, one loader for every door onto it: the `lazy()` in `App.tsx` and
 * `live/LiveApp.tsx`, and the board's prefetch on mount (`pages/Tasks.tsx`), so a row press opens a page already
 * downloaded. A module of its own: importing it from `App.tsx` would cycle (`App.tsx` lazily loads the board), and
 * from the board would pull the board into the shell's chunk. One module record per URL, so a second call costs
 * nothing. */
export const loadTaskDetailPage = () => import('./taskDetail/TaskDetailPage')
