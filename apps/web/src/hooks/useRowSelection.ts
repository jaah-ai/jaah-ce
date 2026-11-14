import { useCallback, useEffect, useState } from 'react'
import { toggled } from '../lib/sets'

/* A list page's bulk selection: picked row ids, PRUNED to `shownIds` (the rows on screen, memoised by the
 * caller). A row a filter dropped, a delete removed, a page turn or a folded group hid must leave the pick,
 * or a bulk verb acts on rows nobody can see; so "select all" is the shown rows. The prune returns early when
 * nothing is picked or every pick is shown, so a keystroke does not re-render the table. `set` is for a
 * caller that hands back a whole next Set. The ink is `ui/RowSelect.tsx`. */
export function useRowSelection(shownIds: ReadonlySet<string>) {
  const [selected, set] = useState<ReadonlySet<string>>(() => new Set())
  const toggle = useCallback((id: string) => set(prev => toggled(prev, id)), [])
  const clear = useCallback(() => set(new Set()), [])
  useEffect(() => {
    if (selected.size === 0) return
    if ([...selected].every(id => shownIds.has(id))) return
    set(prev => new Set([...prev].filter(id => shownIds.has(id))))
  }, [shownIds, selected])
  const allShownSelected = shownIds.size > 0 && [...shownIds].every(id => selected.has(id))
  const someShownSelected = [...selected].some(id => shownIds.has(id))
  const toggleAll = () => set(allShownSelected ? new Set() : new Set(shownIds))
  return { selected, set, toggle, clear, toggleAll, allShownSelected, someShownSelected }
}

/** What the hook returns: a page hands it whole to the table that draws the ticks (`components/WorktreePool.tsx`). */
export type RowSelection = ReturnType<typeof useRowSelection>
