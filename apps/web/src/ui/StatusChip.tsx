import Chip from '@mui/material/Chip'
import type { JSX, ReactNode } from 'react'
import { PRESS_SWAP_SX, PressSwap, type PressFace } from './pressSwap'

/* The console's one status pill: a stock MUI `Chip`, small and outlined. It knows no domain vocabulary
 * and must never grow a `state` prop — state→tone lives in `statusTone.ts` (tasks:
 * `components/tasks/shared.tsx`). This file owns only the shape. */
export type Tone = 'neutral' | 'info' | 'success' | 'warning' | 'error'

export function StatusChip({
  label,
  tone,
  glyph,
  title,
  ariaLabel,
  onClick,
  expanded,
  className,
  glyphTestId,
  labelTestId,
  iconOnly = false,
  press,
  'data-status': dataStatus,
}: {
  label: string
  tone: Tone
  /** A shape signal beside the label, so the status survives greyscale and forced-colours mode. A node,
   *  not only a string: the task list renders a spinner here for an in-flight row. */
  glyph?: ReactNode
  /** The full sentence — tooltip, and the accessible name unless `ariaLabel` overrides it. */
  title?: string
  /** The accessible name when it must differ from `title`: a clickable pill is named for what pressing it
   *  does. */
  ariaLabel?: string
  /** Makes the pill a real button (MUI's `clickable` Chip, so focus ring and Enter/Space come with it). */
  onClick?: () => void
  /** A clickable pill that shows or hides something says which (`aria-expanded`). */
  expanded?: boolean
  /** Escape hatch for a caller with its own page-scoped `sx` rule keyed on a class. */
  className?: string
  /* Test ids for the glyph/label spans: the `/tasks` card layout hides the label and shows the glyph at
  phone width. `data-testid`, not a class, so callers cannot restyle the pill. */
  glyphTestId?: string
  labelTestId?: string
  /** Render the glyph alone: the label stays the accessible name (unless `title`/`ariaLabel` say more) and
   *  the pill becomes a `role="img"`, so a screen reader still hears the word the eye no longer reads. */
  iconOnly?: boolean
  /** What a clickable pill shows while hovered or focused: the action's glyph and word in place of the
   *  status's (`./pressSwap.tsx`). Ignored without `onClick`; `iconOnly` swaps the glyph alone. */
  press?: PressFace
  /** A machine-readable status token for tests and page-scoped rules; the vocabulary is the caller's. */
  'data-status'?: string
}): JSX.Element {
  const rest =
    iconOnly && glyph ? (
      <span data-testid={glyphTestId} aria-hidden="true">{glyph}</span>
    ) : glyph ? (
      <>
        <span data-testid={glyphTestId} aria-hidden="true">{glyph}</span>
        <span data-testid={labelTestId}>{label}</span>
      </>
    ) : (
      label
    )
  const swap = press !== undefined && onClick !== undefined
  return (
    <Chip
      size="small"
      variant="outlined"
      /* A tone IS a MUI palette colour; `neutral` is MUI's `default` grey outline, its label muted (below) so it
         reads quieter than the coloured tones beside it rather than as bold primary text. */
      color={tone === 'neutral' ? 'default' : tone}
      className={className}
      data-status={dataStatus}
      title={title}
      aria-label={ariaLabel ?? title ?? (iconOnly && glyph ? label : undefined)}
      /* Spread, never `role={undefined}`: an explicit undefined overrides the `role="button"` MUI sets on a
         clickable Chip. */
      {...(iconOnly && glyph && !onClick ? { role: 'img' } : {})}
      onClick={onClick}
      aria-expanded={expanded}
      label={swap ? <PressSwap rest={rest} press={iconOnly && glyph ? { glyph: press.glyph } : press} /> : rest}
      /* Density: both numbers are reductions from MUI's stock small Chip (24px, 8px label padding). */
      sx={{
        height: 20,
        /* A status pill never shrinks its own text: MUI's Chip caps at `max-width: 100%` and ellipsizes, which
        leaves an unreadable stub in a narrow cell. Labels are single short words (`shared.tsx` asserts it), so
        use intrinsic width; a cell too narrow for its status is the cell's problem. */
        maxWidth: 'none',
        // Horizontal padding lives on the root; with MUI's label padding too, both would stack.
        px: 0.75,
        ...(tone === 'neutral' && { color: 'text.secondary' }),
        '& .MuiChip-label': {
          px: 0,
          fontSize: '0.6875rem',
          fontWeight: 600,
          display: 'inline-flex',
          alignItems: 'center',
          gap: 0.5,
          overflow: 'visible',
          flexShrink: 0,
        },
        ...(swap && PRESS_SWAP_SX),
      }}
    />
  )
}

export default StatusChip
