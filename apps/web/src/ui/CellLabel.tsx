import Typography from '@mui/material/Typography'
import type { JSX } from 'react'

import { ABOVE_PHONE } from './listStyles'

/* The column heading carried into the phone card beside its value (the other half of
 * `listStyles.PHONE_CARD_SX`): hiding `thead` removes headings from the accessibility tree.
 * A real element, not a cell `::before`: a pseudo-element is a flex item of the cell and would centre
 * against a wrapping chip cluster; as a child it leads the same wrap flow.
 * No height cost; `display: none` above the phone, written hide-above (see `ABOVE_PHONE`: environments
 * without media queries see the base). Shared by `/users` and `/projects` on the `PHONE` breakpoint. */
export function CellLabel({ children }: { children: string }): JSX.Element {
  return (
    <Typography
      component="span"
      variant="caption"
      sx={{
        display: 'inline',
        [ABOVE_PHONE]: { display: 'none' },
        color: 'text.secondary',
        whiteSpace: 'nowrap',
        flex: '0 0 auto',
      }}
    >
      {children}
    </Typography>
  )
}

export default CellLabel
