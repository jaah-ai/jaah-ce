import type { JSX } from 'react'
import Box from '@mui/material/Box'

/* THE ONE "NOTHING REPORTED" MARK: a node's Seen, Version and Load on `/nodes`, its Last Seen and
 * Version on the node page, and the empty Health-tab cards all say an absent reading with this word, in this
 * faintness. "Never", "—", a blank, "None recorded" and "None reported" each said the same fact differently.
 * The size is the surface's (it inherits the font); only the word and the colour are fixed here. */
export const NONE = 'None'

export const NONE_SX = { color: 'text.secondary' } as const

export function NoneMark(): JSX.Element {
  return (
    <Box component="span" data-state="none" sx={NONE_SX}>
      {NONE}
    </Box>
  )
}
