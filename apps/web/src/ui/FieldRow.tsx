import type { JSX, ReactNode } from 'react'
import Box from '@mui/material/Box'
import type { SxProps, Theme } from '@mui/material/styles'

/* Horizontal field-grouping row. Wrapping is content-driven: each cell's `flex-basis` (set at the call
 * site, in rem) decides how many share a line, not a media query, so a growing label makes a cell taller
 * and never truncates it. */
export function FieldRow({
  children,
  align = 'start',
  columnGap = '1rem',
  rowGap,
  testId,
  sx,
}: {
  children: ReactNode
  align?: 'center' | 'start'
  columnGap?: string
  /** Left undefined the row emits no `row-gap`, so the flex default (0 plus each cell's own margin) stands. */
  rowGap?: string | number
  testId?: string
  sx?: SxProps<Theme>
}): JSX.Element {
  return (
    <Box
      data-testid={testId}
      sx={[
        {
          display: 'flex',
          flexWrap: 'wrap',
          alignItems: align === 'center' ? 'center' : 'flex-start',
          columnGap,
          ...(rowGap === undefined ? {} : { rowGap }),
        },
        ...(Array.isArray(sx) ? sx : [sx]),
      ]}
    >
      {children}
    </Box>
  )
}

export default FieldRow
