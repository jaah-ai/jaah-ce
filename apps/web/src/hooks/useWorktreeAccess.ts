import { useCan } from '../auth'
import type { ConsoleRequest } from '../components/ConsoleDock'
import { hasLiveSession, resolveTitle } from '../lib/worktree'
import type { Worktree } from '../types'

/* ── The worktree tiers, as the SERVER declares them ──────────────────────────────────────────────
 *
 * `worktrees:view` vs `worktrees:edit` is BEHAVIOURAL: a view principal gets the list and a read-only
 * console; edit gets the two-way terminal and the other actions. (No release control at any tier, and a
 * free slot is unreachable at every tier — see `components/WorktreePool.tsx`.)
 *
 * TWO PREDICATES, BECAUSE THERE ARE TWO SERVER FACTS:
 *   · `POST /api/commands` (worktree commands) accepts `worktrees:edit` OR `system:create` (the bus's
 *     original key, kept) — `useCanEditWorktrees`.
 *   · The console relay accepts `worktrees:edit` ONLY — `useCanDriveWorktreeSession`.
 * Fusing them offered `worktrees:view` + `system:create` a live cursor whose frames the CP discards.
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
 *  `stack_*` / `logs` / `open_finder` / `open_cursor` items; NOT the console keyboard (see
 *  `useCanDriveWorktreeSession`).
 */
export function useCanEditWorktrees(): boolean {
  const can = useCan()
  return can('worktrees:edit') || can('system:create')
}

/** May this principal drive a worktree SESSION — the flat `worktrees:edit` key?
 * The two-way console. `system:create` does NOT pass: the relay (`routes/console.py`) declares
 * `worktrees:edit` alone, so such a principal gets a read-only console. FLAT (`useCan`), exactly as the
 * route checks it (`Principal.has`): it does not ask for the key ON a project. The per-slot half is
 * `worktree_editable_clause` (`tasks:edit` on the running task's project), reported on each row as `may_edit`
 * (`types.ts`), which consumers AND in.
 */
export function useCanDriveWorktreeSession(): boolean {
  return useCan()('worktrees:edit')
}

/** The console request for a pool SLOT — the one place a slot row becomes a `ConsoleRequest`.
 *
 * `mayEdit` is the server's per-row verdict (`types.ts::Worktree.may_edit`) forwarded VERBATIM: the console
 * ANDs it with `useCanDriveWorktreeSession` and fails closed on anything but `true` (`ConsoleTerminal.tsx`).
 * `nodeId`: (project, number) names one slot PER NODE; without it the relay may attach another node's
 * same-numbered slot. `title`: the resolved Title at open time
 * (`lib/worktree.ts::resolveTitle`). `hadLiveSession`: the row's session answer at click time, for the
 * pane's wording only (`ConsolePane.tsx`'s SLOT_EMPTY_REASON). Not for a MicroVM view (no slot).
 */
export function slotConsoleRequest(wt: Worktree): ConsoleRequest {
  return {
    project: wt.project,
    projectId: wt.project_id,
    number: wt.number,
    nodeId: wt.node_uuid,
    mayEdit: wt.may_edit,
    title: resolveTitle(wt),
    hadLiveSession: hasLiveSession(wt),
  }
}
