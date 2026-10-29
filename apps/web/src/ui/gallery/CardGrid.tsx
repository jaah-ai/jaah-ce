import Box from '@mui/material/Box'
import { Children, type JSX, type ReactNode } from 'react'
import { PHONE } from '../listStyles'

/* THE GALLERY'S GRID — the console's one card grid (the style guide's Gallery archetype).
 * As many 260px-or-wider columns as fit, sharing the spare width; one column at phone width. Each child is one
 * card (`ui/gallery/GalleryCard`), announced as a list item. */
export const CARD_MIN_WIDTH = 260

export default function CardGrid({ children, label, testId }: {
  children: ReactNode
  /** What the cards are, for a screen reader ("Most popular"). */
  label: string
  testId?: string
}): JSX.Element {
  return (
    <Box
      role="list"
      aria-label={label}
      data-testid={testId}
      sx={{
        display: 'grid',
        gridTemplateColumns: `repeat(auto-fill, minmax(${CARD_MIN_WIDTH}px, 1fr))`,
        gap: 2,
        minWidth: 0,
        [PHONE]: { gridTemplateColumns: 'minmax(0, 1fr)' },
      }}
    >
      {Children.map(children, child => child == null || child === false ? null : (
        <Box role="listitem" sx={{ minWidth: 0, display: 'flex' }}>{child}</Box>
      ))}
    </Box>
  )
}
