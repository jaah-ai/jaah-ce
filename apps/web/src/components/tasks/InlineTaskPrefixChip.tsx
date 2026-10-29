import Box from '@mui/material/Box'
import Chip from '@mui/material/Chip'
import type { SxProps, Theme } from '@mui/material/styles'
import type { InlineTaskPrefix } from './inlineTaskPrefix'
import { SR_ONLY_SX } from '../../ui/listStyles'

/** DOM id of the hidden hint, for the create box's `aria-describedby`. One id is safe: the host mounts
 *  exactly one create row. */
export const INLINE_PREFIX_HINT_ID = 'inline-new-task-prefix-hint'

/* The typed `Chat:` prefix as a chip: the create row is in advisor-chat mode. Not `ui/StatusChip` —
 * that pill is inert by contract, and this one is removable. Drawn by `FeedNewTaskRow`, which
 * passes its scale via `sx`. */
export default function InlineTaskPrefixChip({
  prefix,
  onClear,
  disabled = false,
  sx,
}: {
  prefix: InlineTaskPrefix
  /** Drop the chip. The typed text stays. */
  onClear: () => void
  /** True while a create is in flight, so the mode cannot change under a sent write. */
  disabled?: boolean
  sx?: SxProps<Theme>
}) {
  return (
    <>
      {/* The escape in the accessibility tree (`title` reaches neither keyboard nor touch), via the create
          box's `aria-describedby`. */}
      <Box component="span" id={INLINE_PREFIX_HINT_ID} sx={SR_ONLY_SX}>{prefix.clearHint}</Box>
      <Chip
        size="small"
        color="primary"
        data-testid="inline-new-task-prefix-chip"
        data-prefix={prefix.word}
        label={prefix.label}
        /* The × is the touch escape; the keyboard one is Backspace at position 0 (`onInputKeyDown`). */
        onDelete={disabled ? undefined : onClear}
        /* Not a tab stop: it would sit between the row and its text box. */
        tabIndex={-1}
        title={prefix.clearHint}
        sx={sx}
      />
    </>
  )
}
