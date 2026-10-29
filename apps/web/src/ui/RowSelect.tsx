import type { ReactNode } from 'react'
import Checkbox from '@mui/material/Checkbox'
import type { FeedSlot } from './feed/types'
import { SELECT_BOX_SX } from './listStyles'

/* A LIST PAGE'S SELECT COLUMN, drawn once: the row's tick, the select-all tick over it, and the slot both sit
 * in. The picks live in `hooks/useRowSelection.ts`; this file is only their ink, so every roster (nodes, users,
 * worktree slots, credentials) ticks the same 24px box in the same 26px slot and names itself the same way. */

/** The Select slot: a 24px box (`SELECT_BOX_SX`) plus air. */
export const SELECT_SLOT = 26

/** A row's tick, named after the row (`Select ${name}`). */
export function SelectRowBox({ testId, checked, onChange, name }: {
  testId: string
  checked: boolean
  onChange: () => void
  /** What the tick picks, as the row names itself (a node's name, a user's email, `acme#2 — on nodeA`). */
  name: string
}) {
  return (
    <Checkbox
      size="small"
      data-testid={testId}
      checked={checked}
      onChange={onChange}
      slotProps={{ input: { 'aria-label': `Select ${name}` } }}
      sx={SELECT_BOX_SX}
    />
  )
}

/** The select-all tick: three states (all / none / some → `indeterminate`), since a partial selection must not
 *  render as empty. Its name flips with its meaning and states the count it acts on (`shown`: "3 nodes on this
 *  page"); the same sentence rides `title`, as the header cell carries no word. */
export function SelectAllBox({ testId, allSelected, someSelected, onChange, shown }: {
  testId: string
  allSelected: boolean
  someSelected: boolean
  onChange: () => void
  shown: string
}) {
  const label = allSelected ? `Clear the selection of all ${shown}` : `Select all ${shown}`
  return (
    <Checkbox
      size="small"
      data-testid={testId}
      checked={allSelected}
      indeterminate={someSelected && !allSelected}
      onChange={onChange}
      slotProps={{ input: { 'aria-label': label, title: label } }}
      sx={SELECT_BOX_SX}
    />
  )
}

/** The row's Select slot, last in its cluster: `compact`, so the one control never lifts the row off its 24px
 *  band. `gate` for a column a width hides (`ui/feed/geometry.ts`); absent, the tick shows at every width — a
 *  bulk verb must stay reachable. */
export function selectSlot(testId: string, node: ReactNode, gate?: FeedSlot['gate']): FeedSlot {
  return { key: 'select', width: SELECT_SLOT, align: 'center', compact: true, testId, gate, node }
}
