import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import type { JSX, ReactNode } from 'react'

/* A frameless settings section's header row: a `0.9rem/600` heading and/or a caption on the left, one control
 * (an Add/New button, a status chip) on the right. It wraps, so at phone width the control takes its own line.
 * For a framed panel use `SectionCard`. */
export function SectionHeader({
  title,
  titleId,
  titleTestId,
  caption,
  action,
  basis = '20rem',
}: {
  title?: ReactNode
  /** For the section's `aria-labelledby`. */
  titleId?: string
  titleTestId?: string
  caption?: ReactNode
  action?: ReactNode
  /** The text column's flex basis: below it, `action` wraps onto its own line. */
  basis?: string
}): JSX.Element {
  return (
    <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'flex-start', justifyContent: 'space-between', columnGap: 1.5, rowGap: 1 }}>
      <Box sx={{ flex: `1 1 ${basis}`, minWidth: 0, maxWidth: '72ch' }}>
        {title != null && (
          <Typography id={titleId} data-testid={titleTestId} variant="subtitle1" component="h3" sx={{ fontSize: '0.9rem', fontWeight: 600 }}>
            {title}
          </Typography>
        )}
        {caption && <Typography variant="body2" sx={{ color: 'text.secondary' }}>{caption}</Typography>}
      </Box>
      {action}
    </Box>
  )
}

export default SectionHeader
