/* A PINNED BOARD ROW'S MARK (`lib/taskPin.ts`), beside its name at every width — and its one-click Unpin. Pin itself
 * is the ⋯'s item: a mark on every row would be noise. */
import ButtonBase from '@mui/material/ButtonBase'
import { FONT_SIZE, LINE_HEIGHT, RADIUS, grid } from '../../theme'
import Tip from '../../ui/Tip'
import { ACTION_ICON } from './shared'

// The row's 30px band: never under the 24px tap floor (WCAG 2.5.8), never taller than the row.
const MARK_SX = {
  minWidth: grid(4),
  minHeight: grid(4),
  px: 0.5,
  borderRadius: RADIUS.sm,
  fontSize: FONT_SIZE.sm,
  lineHeight: LINE_HEIGHT.normal,
  '&:hover, &.Mui-focusVisible': { bgcolor: 'action.hover' },
} as const

export default function PinnedMark({ title, onUnpin }: {
  /** The name the row shows, for the button's accessible name. */
  title: string
  onUnpin: () => void
}) {
  return (
    <Tip title="Unpin">
      <ButtonBase aria-label={`Unpin ${title}`} data-testid="task-pinned-mark" onClick={onUnpin} sx={MARK_SX}>
        {ACTION_ICON.pin}
      </ButtonBase>
    </Tip>
  )
}
