import type { Theme } from '@mui/material/styles'
import SearchBox from '../../../ui/SearchBox'
import { PHONE } from '../../../ui/listStyles'
import { grid } from '../../../theme'

/* THE `/tasks` SEARCH BOX, in the pane header where the breadcrumb was (it left the Filter & view panel).
 * The term is the URL's `?q=`, words the server finds in any order (`TaskToolbar`'s `q` prop). The debounce,
 * Enter, ✕/Escape and the sync-back are `ui/SearchBox`'s; here, no max-wait: a query mid-word is the thing
 * the delay exists to avoid. ✕ and Escape clear as a navigation (`onClear`). */

export const SEARCH_DELAY_MS = 500

export default function TaskSearchBox({
  value,
  onChange,
  onClear,
  autoFocus,
  hintBelow = false,
}: {
  /** The committed term, verbatim. */
  value: string
  /** Typing, once it pauses (replaces the history entry). */
  onChange: (next: string) => void
  /** Removing the term (pushes). */
  onClear: () => void
  /** Focus on mount: the phone opens the box from its search button (`PaneHeader`). */
  autoFocus?: boolean
  /** The hint opens below: the phone's box sits under the header line, whose buttons a hint above would cover. */
  hintBelow?: boolean
}) {
  return (
    <SearchBox
      testId="task-search"
      value={value}
      onChange={onChange}
      onClear={onClear}
      autoFocus={autoFocus}
      hintPlacement={hintBelow ? 'bottom-start' : 'top-start'}
      delayMs={SEARCH_DELAY_MS}
      ariaLabel="Search tasks"
      placeholder="Search tasks"
      title="Number, or words in title, description or outcome"
      sx={(t: Theme) => ({
        flex: `0 1 ${grid(53)}`,
        // 150px, the readable floor `e2e/tasks-feed.spec.ts` holds it to: any wider and a 1024px window's split
        // pane (a ~348px board) wraps the action cluster under the box.
        minWidth: grid(25),
        [PHONE]: { flex: `1 1 ${grid(21)}`, minWidth: 0, '& .MuiInputBase-root': { minHeight: t.custom.tapTargetPx } },
      })}
    />
  )
}
