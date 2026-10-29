import { post } from '../api'
import type { Worktree } from '../types'
import { isUserOwned, slotIdentity, userOwnerLabel } from './poolSlot'

// Ending one slot's hold, shared by the pool panel (`components/WorktreePool.tsx`, operator) and the node page's
// Held cell (`pages/NodeDetailPage.tsx`, anyone with `worktrees:edit` who may drive the slot): the request,
// the confirm's words and the outcome's words, so both surfaces say the same thing.

/* End one slot's hold — `POST /api/worktrees/{project_id}/{number}/release`, 204
   (`app/routes/pool_slots.py::release_slot`). Idempotent for a principal who may drive the slot; a narrow
   `worktrees:edit` reader gets 404 on a free slot, so Release is offered only on held rows.
   Gate: `worktrees:edit` + the CP's per-slot editable probe (not superadmin). Not harmless: a `task`
   hold's release ENDS ITS RUNNER and the next lease resets the tree with no stash. Safety is the CP's 409
   on a freshly-live session; `force` waives only that and is relayed to the node (whose `_release` would
   otherwise refuse before killing the window). A 2xx means the hold is gone even if the node is unreachable.
   **THE BULK CALLER NEVER FORCES, AND MUST NOT BE ABLE TO**: bulk is unattended; only a confirm with one
   slot in view offers the waiver. */
export function releaseSlot(slot: Worktree, force = false): Promise<void> {
  return post<void>(
    `/api/worktrees/${slot.project_id}/${encodeURIComponent(String(slot.number))}/release` +
    `?node_id=${encodeURIComponent(slot.node_uuid)}`,
    // Body only when forcing: an absent body IS `force=false` (`ReleaseSlotIn`).
    force ? { force: true } : undefined,
  )
}

/** A task hold (not a person's): its release ends the task's runner. */
function endsARunner(slot: Worktree): boolean {
  return slot.task_id !== null && !isUserOwned(slot)
}

/** The release confirm's sentence for `slot` on the node named `node`. The cost differs by holder. A TASK hold
 *  ends its runner (a durable `release_slot` intent, `routes/pool_slots.py::_mint_release_teardown`; the node's
 *  `_clear_tree` signals every process). A USER hold mints nothing. The runner's end is the one non-recoverable
 *  part, so it is named. `dirty === true` maps to TRACKED edits a reset loses (committed work is backed up under
 *  `refs/pool-unpushed/…`, untracked files survive); `null` never reads as clean. */
export function releaseConfirmMessage(slot: Worktree, node: string): string {
  const key = slotIdentity(slot)
  const held = isUserOwned(slot)
    ? `It is held by ${userOwnerLabel(slot)}.`
    : slot.task_id !== null
      ? `It is held by task #${slot.task_id}.`
      : 'It is held by a task.'
  const runner = endsARunner(slot)
    ? ` Task #${slot.task_id}'s runner is ended with it — its processes and its terminal window are ` +
      'torn down, and the task itself is left active for you to decide about.'
    : ''
  const work = slot.dirty === true
    ? ' This slot has uncommitted changes, which that reset discards with no stash or backup.'
    : ''
  return `Release ${key}? ${held} The hold ends and the slot becomes free for the next dispatch.${runner} ` +
    `Nothing on ${node} is deleted right now — the slot, its files and its ` +
    `stack all stay exactly as they are. The next dispatch into this slot resets the tree.${work}`
}

/** What a successful release did, for `slot` on the node named `node`. A forced one says what was WAIVED, as
 *  the on-screen record of the decision; "untouched" is said only for a user hold. */
export function releasedNotice(slot: Worktree, node: string, force: boolean): string {
  const key = slotIdentity(slot)
  if (force) {
    return `Released ${key} over a live session — the hold has ended, the session was torn down, and ` +
      `the tree on ${node} is reset by the next dispatch.`
  }
  return endsARunner(slot)
    ? `Released ${key} — the hold has ended and task #${slot.task_id}'s runner on ` +
      `${node} is torn down. The tree is reset by the next dispatch.`
    : `Released ${key} — the hold has ended and the slot on ${node} is untouched.`
}
