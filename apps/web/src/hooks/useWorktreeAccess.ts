import { useCan } from '../auth'

/* ── The worktree tiers, as the SERVER declares them ──────────────────────────────────────────────
 *
 * `worktrees:view` vs `worktrees:edit` is BEHAVIOURAL: a view principal gets the list and a read-only
 * console; edit gets the two-way terminal and the other actions. (No release control at any tier, and a
 * free slot is unreachable at every tier — see `components/WorktreePool.tsx`.)
 *
 * TWO PREDICATES, BECAUSE THERE ARE TWO SERVER FACTS:
 *   · `POST /api/commands` (worktree commands) accepts `worktrees:edit` OR `system:create` (the bus's
 *     original key, kept) — `useCanEditWorktrees`.
 *   · The console relay and `PUT …/{project}/{number}/title` accept `worktrees:edit` ONLY —
 *     `useCanDriveWorktreeSession`.
 * Fusing them offered `worktrees:view` + `system:create` a live cursor whose frames the CP discards, plus
 * renames that 403.
 *
 * Each mirrors ONE route declaration literally (no implication logic — settled); hence no
 * `canViewWorktrees` either: `worktrees:view` is spelled by `App.tsx`'s `RequireFeature`, `Sidebar.tsx`,
 * and each fetching page. A group granted only `edit` cannot load the list, on purpose. Superadmin is
 * allow-all (`useCan`).
 *
 * PRESENTATION ONLY, NEVER THE SECURITY BOUNDARY: the relay drops a viewer's frames, the daemon uses
 * `tmux -r`, and both routes refuse the write.
 */

/** May this principal drive the node COMMAND BUS — `worktrees:edit` OR `system:create`? The ⋯ sheet's
 *  `stack_*` / `sync_develop` / `logs` / `open_finder` / `open_cursor` items and the Sync fan-outs; NOT the
 *  console keyboard or Title rename (see `useCanDriveWorktreeSession`).
 */
export function useCanEditWorktrees(): boolean {
  const can = useCan()
  return can('worktrees:edit') || can('system:create')
}

/** May this principal drive a worktree SESSION — the flat `worktrees:edit` key?
 * The two-way console and the Title rename. `system:create` does NOT pass: the relay
 * (`routes/console.py`) and title route (`routes/pool_slots.py`) declare `worktrees:edit` alone, so such a
 * principal gets a read-only console. FLAT (`useCan`), exactly as both routes check it (`Principal.has`):
 * neither asks for the key ON a project. The per-slot half is `worktree_editable_clause` (`tasks:edit` on
 * the running task's project, or the caller's own hold), reported on each row as `may_edit`
 * (`types.ts`), which consumers AND in.
 */
export function useCanDriveWorktreeSession(): boolean {
  return useCan()('worktrees:edit')
}
