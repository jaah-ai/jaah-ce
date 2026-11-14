import Box from '@mui/material/Box'
import { Children, type JSX, type ReactNode } from 'react'
import { GALLERY_CONTAINER, GALLERY_NARROW, PHONE } from '../listStyles'
import { GALLERY_NARROW_COLUMNS, GALLERY_ROW_COLUMNS } from './GalleryCard'
import { RADIUS, grid } from '../../theme'

/* THE GALLERY'S GRID — the console's one card grid (the style guide's Gallery archetype).
 * Cards: as many 258px-or-wider columns as fit, sharing the spare width; one column at phone width. `rows`: one
 * framed column of `ui/gallery/GalleryCard` rows on shared columns, divided by hairlines. Each child is one item,
 * announced as a list item. */

export default function CardGrid({ children, label, testId, rows = false }: {
  children: ReactNode
  /** What the cards are, for a screen reader ("Most popular"). */
  label: string
  testId?: string
  /** One row per item (`GalleryCard`), not a grid of cards. */
  rows?: boolean
}): JSX.Element {
  const items = Children.map(children, child => child == null || child === false ? null : (
    <Box role="listitem" sx={{ minWidth: 0, display: 'flex' }}>{child}</Box>
  ))
  if (!rows) {
    return (
      <Box
        role="list"
        aria-label={label}
        data-testid={testId}
        sx={{
          display: 'grid',
          gridTemplateColumns: `repeat(auto-fill, minmax(${grid(43)}, 1fr))`,
          gap: 2,
          minWidth: 0,
          [PHONE]: { gridTemplateColumns: 'minmax(0, 1fr)' },
        }}
      >
        {items}
      </Box>
    )
  }
  return (
    // The rows stack by this box's width (`GALLERY_NARROW`), so a narrow pane stacks them as a phone does.
    <Box sx={{ containerType: 'inline-size', containerName: GALLERY_CONTAINER, minWidth: 0 }}>
      <Box
        role="list"
        aria-label={label}
        data-testid={testId}
        sx={{
          // Every row on the same columns (a subgrid), so names, summaries and verbs line up down the list.
          display: 'grid',
          gridTemplateColumns: GALLERY_ROW_COLUMNS,
          minWidth: 0,
          border: 1,
          borderColor: 'divider',
          borderRadius: RADIUS.sm,
          '& > [role=listitem]': { gridColumn: '1 / -1', display: 'grid', gridTemplateColumns: 'subgrid' },
          '& > [role=listitem] > *': { gridColumn: '1 / -1', gridTemplateColumns: 'subgrid' },
          '& > [role=listitem] + [role=listitem]': { borderTop: 1, borderColor: 'divider' },
          [GALLERY_NARROW]: {
            display: 'flex',
            flexDirection: 'column',
            '& > [role=listitem]': { display: 'flex' },
            '& > [role=listitem] > *': { gridTemplateColumns: GALLERY_NARROW_COLUMNS },
          },
        }}
      >
        {items}
      </Box>
    </Box>
  )
}
