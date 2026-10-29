/* THE COST BADGE — a Task's (and its children's) cost as one whole-dollar figure, rounded up, with the exact
 * figure on hover; drawn by every task row (`listViews/feedRow.tsx`) so one task never
 * shows two figures. Rounding lives in `lib/taskCost.ts` (its single home). Conventions follow `RatingBadge.tsx`.
 *
 * IDENTITY HERE, GEOMETRY AT THE HOST (see `AuthorBadge.tsx`): host `sx` applies after the layout
 * defaults (size, line height, right alignment — overridable) and before the identity (mono face,
 * weight, neutral colour — not overridable). Money right-aligns so digits share a column.
 * NOT ARIA-HIDDEN: the number IS the fact.
 * DRAWS NOTHING WHEN THERE IS NO COST (never `$0.00`: absence is not
 * a state).
 * A `partial` cost renders plainly (a marker would decorate much of the column). `ui/FeedList.tsx` exposes the badge as its own cell, so no host restates it.
 * AN AGENT-REPORTED COST IS MARKED (`*`, explained on hover by `costTitle`; Efficiency's "agent-reported"): it is
 * the agent's own claim, not a price from tokens, and rare. `$999,999*` is nine characters, the slot's budget.
 */
import Box from '@mui/material/Box'
import type { SxProps, Theme } from '@mui/material/styles'
import { costLabel, costTitle } from '../../lib/taskCost'
import type { PipelineTask } from '../../types'
import { monoSx } from '../../ui/listStyles'
import { VISUALLY_HIDDEN } from '../../ui/visuallyHidden'

export default function CostBadge({
  task,
  sx,
  title,
  testId = 'task-cost-badge',
}: {
  /**
   * A `Pick`, so narrower row types satisfy it without a cast. Semantics are single-homed on `PipelineTask`.
   * THE PRESENCE TEST IS `hasCost`, not either field alone.
   */
  task: Pick<PipelineTask, 'cost_usd' | 'cost_status' | 'cost_agent_reported'>
  /** Host geometry (width, margins, type scale, alignment); cannot repaint the identity. */
  sx?: SxProps<Theme>
  /**
   * OVERRIDE the `title` sentence, or `null` for none. `undefined` derives `costTitle`. `null` is for a
   * host wrapping this in a MUI `Tooltip`, which would otherwise show a second native tooltip on the
   * descendant (same trap as `RatingBadge`'s prop).
   */
  title?: string | null
  /** Spec handle, per badge. */
  testId?: string
}): JSX.Element | null {
  const shown = costLabel(task)
  /* `''` covers every don't-know reading (see `hasCost`); `null` leaves the host cell `:empty`. */
  if (shown === '') return null

  return (
    <Box
      component="span"
      data-testid={testId}
      /* The exact figure (`costTitle`). `?? undefined`, not `''`: some readers announce an empty `title` as a label. */
      title={(title === undefined ? costTitle(task) : title) ?? undefined}
      sx={[
        {
          /* LAYOUT DEFAULTS (overridable). `inline-block` so `textAlign` works when stretched;
             `lineHeight: 1` keeps height equal to font size. */
          display: 'inline-block',
          fontSize: '0.8125rem',
          lineHeight: 1,
          textAlign: 'right',
          // Separators would otherwise let a narrow cell break `$1,` from the rest.
          whiteSpace: 'nowrap',
        },
        ...(Array.isArray(sx) ? sx : [sx]),
        // MUI's `sx` merge idiom (see `AuthorBadge.tsx`): a nested array is not a legal entry.
        theme => ({
          /* MONO: a measurement is mono (`TaskRunMetrics.tsx`), and tabular digits align decimals.
             `monoSx` — `ui/listStyles.ts` is the stack's single home. */
          ...monoSx(theme),
          fontWeight: 600,
          /* NEUTRAL: cost is data, not status; fifty coloured rows emphasise nothing. */
          color: 'text.primary',
        }),
      ]}
    >
      {shown}
      {task.cost_agent_reported === true && (
        <>
          <Box component="span" aria-hidden data-testid={`${testId}-agent-reported`} sx={{ color: 'text.secondary' }}>
            *
          </Box>
          {/* The marker's meaning for a screen reader, which may not announce `title`. */}
          <Box component="span" sx={VISUALLY_HIDDEN}> agent-reported</Box>
        </>
      )}
    </Box>
  )
}
