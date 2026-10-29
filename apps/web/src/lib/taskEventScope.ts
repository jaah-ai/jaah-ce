import type { ConsoleEvent } from '../hooks/useEventStream'

/* CAN THIS `task` EVENT HAVE MOVED A VIEW over `scope` — the ids it draws? `task` has 44 emit sites
   (`hooks/useEventStream.ts`), so a view that re-reads on every one of them re-reads per fleet event; on a busy
   fleet that spent the public WAF's per-IP budget. `scope` is null before a first answer. */
export function touchesScope(event: ConsoleEvent, scope: ReadonlySet<number> | null): boolean {
  if (scope === null) return true // first load, or a retry after one that failed — nothing to filter on
  /* Births always pass (a new child joins the subtree and carries no parent). `draft` is what the two
     child-create sites emit (`_seed_epic_children` emits nothing). */
  if (event.action === 'created' || event.action === 'draft') return true
  const id = Number(event.id)
  // An unreadable id is a don't-know, and a don't-know refetches.
  return !Number.isFinite(id) || scope.has(id)
}
