// A PANE'S WINDOW BUTTONS — Maximize/Restore and Close, as the web console's header draws them
// (components/ConsoleTerminal.tsx) and the list/detail pane reuses them (components/ListDetail.tsx). One
// glyph pair, one button style, one naming rule ("<Verb> <noun>"), so the two panes read as one system.
import IconButton from '@mui/material/IconButton'
import { CloseIcon, MaximizeIcon, RestoreIcon } from '../ui/icons'

/** Every pane header button: the console's chevrons, ↻, ⚙, ▦, ⚡ and these two. */
export const paneHeaderButtonSx = {
  minWidth: '2rem',
  fontSize: '1rem',
  lineHeight: 1,
  borderRadius: 1,
  color: 'text.secondary',
  '&:hover:not(:disabled)': { color: 'text.primary' },
  '&:disabled': { color: 'text.disabled' },
} as const

/** `title` + `aria-label` on each, since a glyph alone cannot say what it does. */
export function MaximizeButton({
  noun,
  maximized,
  onToggle,
}: {
  noun: string
  maximized: boolean
  onToggle: () => void
}) {
  const label = `${maximized ? 'Restore' : 'Maximize'} ${noun}`
  return (
    <IconButton sx={paneHeaderButtonSx} onClick={onToggle} aria-label={label} title={label}>
      {maximized ? <RestoreIcon /> : <MaximizeIcon />}
    </IconButton>
  )
}

export function CloseButton({ noun, onClose }: { noun: string; onClose: () => void }) {
  return (
    <IconButton sx={paneHeaderButtonSx} onClick={onClose} aria-label={`Close ${noun}`} title={`Close ${noun}`}>
      <CloseIcon />
    </IconButton>
  )
}
