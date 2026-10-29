import Box from '@mui/material/Box'
import Card from '@mui/material/Card'
import CardContent from '@mui/material/CardContent'
import Typography from '@mui/material/Typography'
import { Children } from 'react'
import type { JSX, ReactNode } from 'react'

/* The body's (and title row's) padding, in theme spacing units (8px each); `dense` takes 1. */
const BODY_PAD = 1.5

/** Where a card's text starts from its outer edge — the body padding plus the 1px outline: what a line set
 *  beside a stack of cards (Settings' read-only note) is inset by, so it aligns with the cards' text. */
export const SECTION_CARD_TEXT_INSET = `${BODY_PAD * 8 + 1}px`

/* The console's one panel. `outlined`, never elevated: stacked elevated panels read as noise (AC4e).
 * No `CardHeader`: its padding and `h5` title are taller than this header, a measured criterion (AC5).
 * Clips with `clip`, not MUI's `hidden`: `hidden` makes the card a scroll container, so a sticky child (the form
 * action bar, `ui/FormActionBar`) stuck to the card and sat in the flow, under the phone's bottom nav, on a form
 * taller than the screen. `clip` clips the same corners without being one, so the bar sticks to `.app-main`.
 * `minWidth: 0` keeps the scroll container's shrink: as a flex or grid item the card never widens to a wide child. */
export function SectionCard({
  title,
  tip,
  actions,
  children,
  dense,
  testId,
}: {
  title?: ReactNode
  /** An ⓘ (`ui/InfoTip`) qualifying the title, right after it — outside the heading, so the heading's
   *  accessible name stays the title alone. Shown only with a `title`. */
  tip?: ReactNode
  /** A control cluster on the title row (an edit button, a filter, a count). */
  actions?: ReactNode
  /** Empty (omitted, `null`, `false`), the card is its title row alone: one line for a section with nothing to list. */
  children?: ReactNode
  /** Halves the body padding, for a card whose whole content is one dense table or list. */
  dense?: boolean
  testId?: string
}): JSX.Element {
  const hasBody = Children.toArray(children).length > 0
  return (
    <Card variant="outlined" data-testid={testId} sx={{ overflow: 'clip', minWidth: 0 }}>
      {(title || actions) && (
        <Box
          sx={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 1,
            px: dense ? 1 : BODY_PAD,
            py: 0.75,
            // No body, no divider: it would double the card's own bottom border.
            borderBottom: hasBody ? 1 : 0,
            borderColor: 'divider',
          }}
        >
          {title && (
            /* The tip's 44px phone tap box is pulled back to the glyph, or it sets the title row's height. */
            <Box sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.5, minWidth: 0, '& > span': { my: { xs: '-12px', sm: 0 } } }}>
              <Typography variant="subtitle2" component="h2" sx={{ fontWeight: 600, minWidth: 0 }}>
                {title}
              </Typography>
              {tip && <Box component="span" sx={{ display: 'inline-flex', mx: { xs: '-12px', sm: 0 } }}>{tip}</Box>}
            </Box>
          )}
          {actions && <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>{actions}</Box>}
        </Box>
      )}
      {/* MUI adds 24px bottom padding to the last CardContent; reset it to match the other sides. */}
      {hasBody && (
        <CardContent sx={{ p: dense ? 1 : BODY_PAD, '&:last-child': { pb: dense ? 1 : BODY_PAD } }}>
          {children}
        </CardContent>
      )}
    </Card>
  )
}

export default SectionCard
