import { useState } from 'react'
import Button from '@mui/material/Button'
import Box from '@mui/material/Box'
import Menu from '@mui/material/Menu'
import MenuItem from '@mui/material/MenuItem'
import { apiDetail } from '../api'
import { useCan } from '../auth'
import { useConfirm } from './ConfirmDialog'
import { refusalMessage, releaseLiveSessionRefusal } from '../lib/erase'
import { isUserOwned, slotIdentity, userOwnerLabel } from '../lib/poolSlot'
import { releaseConfirmMessage, releasedNotice, releaseSlot } from '../lib/slotRelease'
import { Mono } from '../ui/DetailFields'
import type { Worktree } from '../types'

export type ReleaseOutcome = { severity: 'success' | 'error'; text: string }

/** Who holds `slot`, as the menu names it: the task's number or the person. */
function holderLabel(slot: Worktree): string {
  if (isUserOwned(slot)) return userOwnerLabel(slot) ?? 'a person'
  return slot.task_id !== null ? `#${slot.task_id}` : 'a task'
}

/** Why the hold(s) in `locked` have no Release from this seat: the task is not one this seat may change. The Held
 *  cell already names every holder, so the locked ones are named only when some others are releasable. */
function lockedText(locked: Worktree[], all: number): string {
  if (locked.length === all) return '· not yours to release'
  return `· ${[...new Set(locked.map(holderLabel))].join(', ')} not yours to release`
}

/** Release beside the node page's Held cell: the way to end a stuck hold for whoever may drive the slot, now that
 *  the pool page is the operator's. The route's own gate is `worktrees:edit` plus the per-slot editable
 *  probe (`routes/pool_slots.py::release_slot`), mirrored here by the key and each row's `may_edit`; presentation
 *  only. One slot confirms at once; several open a menu to pick which. Words, request and live-session Force are
 *  the pool panel's (`lib/slotRelease.ts`, `lib/erase.ts`). */
export default function ReleaseHeldSlots({
  slots, node, onReleased, onOutcome,
}: {
  slots: Worktree[]
  /** The node's name, as the confirm and outcome say it. */
  node: string
  onReleased: () => void
  onOutcome: (outcome: ReleaseOutcome | null) => void
}) {
  const can = useCan()
  const [confirm, confirmDialog] = useConfirm()
  const [menuAnchor, setMenuAnchor] = useState<HTMLElement | null>(null)
  const [busy, setBusy] = useState(false)
  const releasable = slots.filter(slot => slot.may_edit)
  if (!can('worktrees:edit')) return null
  // A hold this seat may not drive says so, rather than the button silently missing.
  const locked = slots.filter(slot => !slot.may_edit)
  const lockedNote = locked.length > 0 && (
    <Box component="span" data-testid="node-held-locked" sx={{ ml: 1, color: 'text.secondary' }}>
      {lockedText(locked, slots.length)}
    </Box>
  )
  if (releasable.length === 0) return lockedNote || null

  async function release(slot: Worktree) {
    setMenuAnchor(null)
    onOutcome(null)
    const identity = <Mono>{slotIdentity(slot)}</Mono>
    const ok = await confirm({
      title: 'Release slot', identity, message: releaseConfirmMessage(slot, node),
      confirmLabel: 'Release', destructive: true,
    })
    if (!ok) return
    setBusy(true)
    try {
      let force = false
      try {
        await releaseSlot(slot)
      } catch (err) {
        // Force is offered on the live-session refusal alone, never on a status (the main clone also 409s).
        if (!releaseLiveSessionRefusal(err)) throw err
        force = await confirm({
          title: 'Live session', identity, message: refusalMessage(err),
          confirmLabel: 'Force release', destructive: true,
        })
        if (!force) return
        await releaseSlot(slot, true)
      }
      onOutcome({ severity: 'success', text: releasedNotice(slot, node, force) })
      onReleased()
    } catch (err) {
      onOutcome({ severity: 'error', text: refusalMessage(err) || apiDetail(err, 'Failed to release the slot') })
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <Button
        size="small"
        disabled={busy}
        data-testid="node-held-release"
        // One slot: the name says which. Several: the menu does.
        aria-label={releasable.length === 1 ? `Release ${slotIdentity(releasable[0])}` : undefined}
        aria-haspopup={releasable.length > 1 ? 'menu' : undefined}
        aria-expanded={releasable.length > 1 ? menuAnchor !== null : undefined}
        onClick={event => {
          if (releasable.length === 1) void release(releasable[0])
          else setMenuAnchor(event.currentTarget)
        }}
        sx={{ ml: 1, py: 0, minWidth: 0, verticalAlign: 'baseline' }}
      >
        Release
      </Button>
      <Menu anchorEl={menuAnchor} open={menuAnchor !== null} onClose={() => setMenuAnchor(null)}>
        {releasable.map(slot => (
          <MenuItem key={slot.id} onClick={() => void release(slot)} data-testid="node-held-release-item">
            {slotIdentity(slot)} · {holderLabel(slot)}
          </MenuItem>
        ))}
      </Menu>
      {lockedNote}
      {confirmDialog}
    </>
  )
}
