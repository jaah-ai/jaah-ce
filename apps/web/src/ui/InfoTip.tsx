import type { JSX, ReactNode } from 'react'
import { useId } from 'react'
import Box from '@mui/material/Box'
import ButtonBase from '@mui/material/ButtonBase'
import Tooltip, { type TooltipProps } from '@mui/material/Tooltip'
import { SR_ONLY_SX } from './listStyles'

/* ══ THE CONSOLE'S ONE "EXPLAIN THIS" AFFORDANCE ═══════════════════════════════════════════════════
 *
 * A small ⓘ carrying a sentence that is true but not worth a line of the page: prose read once by a
 * newcomer is paid for per reader, not per render.
 *
 * THE PROSE IS NEVER ONLY IN THE TOOLTIP: it is also rendered sr-only beside the trigger and wired via
 * `aria-describedby`, permanently — MUI's `describeChild` describes only while the popper is mounted and
 * only for a string. `descriptionId` lets a control re-use that same copy (authored once).
 *
 * A glyph, not an icon component: `@mui/icons-material` is not a dependency (per
 * `components/tasks/RatingBadge.tsx`, `TaskAxisControls.tsx`).
 *
 * The 44px tap floor at phone width only, in BOTH dimensions (`tapTargetSx` raises only height); above `sm`
 * a 20px box that does not set its caption line's height.
 */
export function InfoTip({
  title,
  label,
  testId,
  descriptionId,
  descriptionTestId,
  placement,
}: {
  /** The explanation, in the tooltip AND sr-only beside the trigger. KEEP IT NON-INTERACTIVE: the sr-only
   *  copy would put an invisible focusable element in the tab order. */
  title: ReactNode
  /** The trigger's accessible NAME — what it is for ("What “silent” means"). */
  label: string
  testId?: string
  /** Id for the sr-only copy, for `aria-describedby` reuse. MUST BE UNIQUE PER INSTANCE (it replaces the
   *  `useId()` fallback): in a list, suffix it with the row's key. */
  descriptionId?: string
  /** Test handle on the sr-only copy. */
  descriptionTestId?: string
  /** Where the tooltip opens; MUI's default (below) when unset. Beside, where below would cover the page's banner. */
  placement?: TooltipProps['placement']
}): JSX.Element {
  const generatedId = useId()
  const hintId = descriptionId ?? `${generatedId}-infotip`
  return (
    /* `position: relative` is load-bearing: `SR_ONLY_SX` is absolute and would otherwise escape
       `.app-layout`'s `overflow: hidden` (a measured scroll leak). */
    <Box
      component="span"
      sx={{ position: 'relative', display: 'inline-flex', alignItems: 'center', verticalAlign: 'middle' }}
    >
      <Tooltip
        title={title}
        placement={placement}
        // Touch has no hover: open at once on long-press; the long dismiss delay lets a remedy be read.
        enterTouchDelay={0}
        leaveTouchDelay={10000}
        // ~42ch keeps the line within the readable measure (`NodeHealth`'s `PROSE_SX` owns the argument).
        slotProps={{ tooltip: { sx: { maxWidth: '42ch' } } }}
      >
        <ButtonBase
          data-testid={testId}
          aria-label={label}
          aria-describedby={hintId}
          /* The tip must not rename its trigger: for a non-string `title`, MUI sets
             `aria-labelledby={open ? id : null}` on the child, which beats `aria-label`. Spelling it
             `undefined` wins the prop merge and React omits it. */
          aria-labelledby={undefined}
          // Explicit `type`: inside the Properties `<form>` a bare button would submit (save the node).
          type="button"
          focusRipple
          sx={theme => ({
            borderRadius: '50%',
            color: 'text.secondary',
            fontSize: '0.8125rem',
            lineHeight: 1,
            flex: '0 0 auto',
            width: { xs: theme.custom?.tapTargetPx ?? 44, sm: 20 },
            height: { xs: theme.custom?.tapTargetPx ?? 44, sm: 20 },
            '&:hover': { color: 'text.primary' },
          })}
        >
          ⓘ
        </ButtonBase>
      </Tooltip>
      <Box component="span" id={hintId} data-testid={descriptionTestId} sx={SR_ONLY_SX}>
        {title}
      </Box>
    </Box>
  )
}

export default InfoTip
