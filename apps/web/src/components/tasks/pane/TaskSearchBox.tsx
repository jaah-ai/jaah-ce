import type { Theme } from '@mui/material/styles'
import SearchBox from '../../../ui/SearchBox'
import { PHONE } from '../../../ui/listStyles'

/* THE `/tasks` SEARCH BOX, in the pane header where the breadcrumb was (it left the Filter & view panel).
 * The term is the URL's `?q=`, one phrase the server matches (`TaskToolbar`'s `q` prop). The debounce,
 * Enter, ✕/Escape and the sync-back are `ui/SearchBox`'s; here, no max-wait: a query mid-word is the thing
 * the delay exists to avoid. ✕ and Escape clear as a navigation (`onClear`). */

export const SEARCH_DELAY_MS = 500

export default function TaskSearchBox({
  value,
  onChange,
  onClear,
}: {
  /** The committed term, verbatim. */
  value: string
  /** Typing, once it pauses (replaces the history entry). */
  onChange: (next: string) => void
  /** Removing the term (pushes). */
  onClear: () => void
}) {
  return (
    <SearchBox
      testId="task-search"
      value={value}
      onChange={onChange}
      onClear={onClear}
      delayMs={SEARCH_DELAY_MS}
      ariaLabel="Search tasks"
      placeholder="Search tasks"
      title="Number, title or body"
      sx={(t: Theme) => ({
        flex: '0 1 20rem',
        // 150px, the readable floor `e2e/tasks-feed.spec.ts` holds it to: any wider and a 1024px window's split
        // pane (a ~348px board) wraps the action cluster under the box.
        minWidth: '150px',
        [PHONE]: { flex: '1 1 8rem', minWidth: 0, '& .MuiInputBase-root': { minHeight: t.custom.tapTargetPx } },
      })}
    />
  )
}
