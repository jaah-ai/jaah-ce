import type { JSX } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import type { SxProps, Theme } from '@mui/material/styles'
import { GLYPH_BOX_SX, TOOLBAR_BUTTON_SX } from './ToolbarMenu'

/* THE CONSOLE'S CREATE CONTROL — a contained, WORDLESS ＋ at the end of a list header's action cluster, as
 * `/tasks` draws it (`components/tasks/TaskToolbar.tsx`, the split button's main half) and Automation's
 * Schedule and Playbooks reuse it. Its name lives on `aria-label` ("New task", "New playbook"), so AT and
 * `getByRole('button'|'link', { name })` find it at every width; `title` gives a pointer the same words.
 * `to` makes it a router link (a create PAGE), `onClick` a button (a dialog or an in-place create). */
export default function NewButton({
  label,
  to,
  onClick,
  testId,
  sx,
}: {
  /** The verb and noun, e.g. "New playbook" — the accessible name and hover text. */
  label: string
  to?: string
  onClick?: () => void
  testId?: string
  /** The caller's own geometry (the Tasks split button squares one side). */
  sx?: SxProps<Theme>
}): JSX.Element {
  const link = to === undefined ? {} : { component: RouterLink, to }
  return (
    <Button
      {...link}
      variant="contained"
      color="primary"
      onClick={onClick}
      aria-label={label}
      title={label}
      data-testid={testId}
      sx={[TOOLBAR_BUTTON_SX, ...(sx === undefined ? [] : Array.isArray(sx) ? sx : [sx])]}
    >
      {/* Wordless, so the glyph takes the label line's box: a bare ＋ drew the button 1.75px short. */}
      <Box component="span" sx={GLYPH_BOX_SX}>＋</Box>
    </Button>
  )
}
