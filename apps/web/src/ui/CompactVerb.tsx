import type { JSX } from 'react'
import Box from '@mui/material/Box'
import type { SxProps, Theme } from '@mui/material/styles'
import { PHONE } from './listStyles'
import { GLYPH_BOX_SX, GLYPH_BUTTON_SX, TOOLBAR_BUTTON_SX, TOOLBAR_LABEL_SX } from './ToolbarMenu'

/* A TAB STRIP'S CREATE VERB (`Tabs` `compactActions`): its word from 481px up, a wordless glyph (＋, or the open
 * toggle's ✕) on a phone, where it shares the strip's row instead of taking a line under it. `wordless` drops the
 * word at every width: a narrow list beside an open record (Projects), where the word would take the
 * strip's line. The host button carries the word on `aria-label` (`compactVerbProps`, the same `wordless`), so AT
 * and `getByRole(…, { name })` find it at every width. */
export function CompactVerbLabel({ label, glyph = '＋', wordless = false }: { label: string; glyph?: string; wordless?: boolean }): JSX.Element {
  return (
    <>
      <Box component="span" aria-hidden="true" sx={[GLYPH_BOX_SX, !wordless && { display: 'none', [PHONE]: { display: 'flex' } }]}>{glyph}</Box>
      {!wordless && <Box component="span" sx={TOOLBAR_LABEL_SX}>{label}</Box>}
    </>
  )
}

/** The host button's props: its name, hover text and the wordless box (44px on a phone; at every width when
 *  `wordless`), then the caller's `sx`. */
export const compactVerbProps = (label: string, sx?: SxProps<Theme>, wordless = false) => ({
  'aria-label': label,
  title: label,
  sx: [wordless ? GLYPH_BUTTON_SX : TOOLBAR_BUTTON_SX, ...(sx === undefined ? [] : Array.isArray(sx) ? sx : [sx])] as SxProps<Theme>,
})
