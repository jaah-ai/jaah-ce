import TableCell from '@mui/material/TableCell'
import TableSortLabel from '@mui/material/TableSortLabel'
import Tip from './Tip'
import type { SxProps, Theme } from '@mui/material/styles'
import type { JSX } from 'react'

/* The console's ONE sortable column heading for a `DataTable`: MUI's `TableSortLabel` (arrow, `aria-sort`, hidden
 * direction announcement) in a right-aligned cell — a sortable column is a figure; `align="left"` for a name column.
 * The caller owns the sort state and its cycle (toggle, or `nextSort`'s first → reversed → the server's order as `null`); an
 * inactive heading points `first` (default desc), the first click's direction. `tip` puts what the heading cannot say on hover. */

export type SortState<K extends string> = { key: K; dir: 'asc' | 'desc' }

/** The three-state heading cycle: `first`, then reversed, then `null` (the server's order). A new key restarts it. */
export function nextSort<K extends string>(current: SortState<K> | null, key: K, first: 'asc' | 'desc' = 'desc'): SortState<K> | null {
  if (current?.key !== key) return { key, dir: first }
  return current.dir === first ? { key, dir: first === 'asc' ? 'desc' : 'asc' } : null
}

/** The two-state heading cycle: the sorted key flips; a new key opens at `first`. */
export function flipSort<K extends string>(current: SortState<K>, key: K, first: 'asc' | 'desc' = 'asc'): SortState<K> {
  return current.key === key ? { key, dir: current.dir === 'asc' ? 'desc' : 'asc' } : { key, dir: first }
}

/** A sorted copy of `rows` by `value`: strings by `localeCompare`, numbers numerically, a missing value last either
 *  way; equal values fall to `tie` (default: the input order — the sort is stable). */
export function rankRows<R>(
  rows: readonly R[],
  value: (row: R) => string | number | null | undefined,
  dir: 'asc' | 'desc',
  tie: (a: R, b: R) => number = () => 0,
): R[] {
  const sign = dir === 'asc' ? 1 : -1
  return [...rows].sort((a, b) => {
    const x = value(a)
    const y = value(b)
    if (x == null || y == null) return x == y ? tie(a, b) : x == null ? 1 : -1
    const order = typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y))
    return sign * order || tie(a, b)
  })
}

export function SortableHead<K extends string>({
  label,
  sortKey,
  sort,
  onSort,
  sx,
  testId,
  align = 'right',
  first = 'desc',
  tip,
}: {
  label: string
  sortKey: K
  sort: SortState<K> | null
  onSort: (key: K) => void
  sx?: SxProps<Theme>
  /** `data-testid` on the label button, for a suite that clicks a column by key rather than by name. */
  testId?: string
  align?: 'left' | 'right'
  /** The inactive heading's direction — the caller's first click. */
  first?: 'asc' | 'desc'
  /** What the heading cannot say itself, on hover. */
  tip?: string
}): JSX.Element {
  const active = sort?.key === sortKey
  const button = (
    <TableSortLabel active={active} direction={active ? sort.dir : first} onClick={() => onSort(sortKey)} data-testid={testId}>
      {label}
    </TableSortLabel>
  )
  return (
    <TableCell align={align} sx={sx} sortDirection={active ? sort.dir : false}>
      {tip ? <Tip title={tip} placement="top" arrow describeChild>{button}</Tip> : button}
    </TableCell>
  )
}
