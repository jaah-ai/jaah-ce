import Chip from '@mui/material/Chip'
import type { JSX, ReactNode } from 'react'
import { FONT_SIZE, FONT_WEIGHT, LINE_HEIGHT, grid } from '../theme'
import { PRESS_SWAP_SX, PressSwap, type PressFace } from './pressSwap'
import Tip from './Tip'

/* The console's one status pill: a stock MUI `Chip`, small and outlined (`dashed` for a provisional one, `wrap`
 * for a label longer than a word). It knows no domain vocabulary and must never grow a `state` prop — state→tone
 * lives in `statusTone.ts` (tasks: `components/tasks/shared.tsx`; compliance: `governance/states.ts`
 * `chipTone`). This file owns only the shape. */
export type Tone = 'neutral' | 'info' | 'success' | 'warning' | 'error'

/** The pill's height: a row mark drawn beside one (the People roster's Admin tag) borrows it. */
export const STATUS_CHIP_HEIGHT = grid(3)

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
  dashed = false,
  wrap = false,
  testId,
  'data-status': dataStatus,
  'data-tone': dataTone,
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
  /** A dashed outline: a status that is provisional or stands apart from the solid ones beside it (a compliance
   *  judge verdict, a fix overlay). The tone still colours it. */
  dashed?: boolean
  /** Let a long label wrap onto a second line instead of overflowing a narrow cell. The default never wraps:
   *  its labels are single short words. */
  wrap?: boolean
  /** The pill's own `data-testid`, where a page has several pills to tell apart. */
  testId?: string
  /** A machine-readable status token for tests and page-scoped rules; the vocabulary is the caller's. */
  'data-status'?: string
  /** The caller's own tone word when its vocabulary is finer than the five tones here (compliance's `judge`
   *  is drawn `neutral` + `dashed` but must stay tellable from `neutral`). Tests and page rules only. */
  'data-tone'?: string
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
    <Tip title={title}><Chip
      size="small"
      variant="outlined"
      /* A tone IS a MUI palette colour; `neutral` is MUI's `default` grey outline, its label muted (below) so it
         reads quieter than the coloured tones beside it rather than as bold primary text. */
      color={tone === 'neutral' ? 'default' : tone}
      className={className}
      data-testid={testId}
      data-status={dataStatus}
      data-tone={dataTone}
      aria-label={ariaLabel ?? title ?? (iconOnly && glyph ? label : undefined)}
      /* Spread, never `role={undefined}`: an explicit undefined overrides the `role="button"` MUI sets on a
         clickable Chip. */
      {...(iconOnly && glyph && !onClick ? { role: 'img' } : {})}
      onClick={onClick}
      aria-expanded={expanded}
      label={swap ? <PressSwap rest={rest} press={iconOnly && glyph ? { glyph: press.glyph } : press} /> : rest}
      /* Density: both values are reductions from MUI's stock small Chip (24px, 8px label padding). */
      sx={{
        height: STATUS_CHIP_HEIGHT,
        /* A status pill never shrinks its own text: MUI's Chip caps at `max-width: 100%` and ellipsizes, which
        leaves an unreadable stub in a narrow cell. Labels are single short words (`shared.tsx` asserts it), so
        use intrinsic width; a cell too narrow for its status is the cell's problem. */
        maxWidth: 'none',
        // Horizontal padding lives on the root; with MUI's label padding too, both would stack.
        px: 0.5,
        ...(tone === 'neutral' && { color: 'text.secondary' }),
        ...(dashed && { borderStyle: 'dashed' }),
        // A wrapping pill grows with its lines and stays inside its cell; the glyph keeps the first line.
        ...(wrap && { height: 'auto', minHeight: STATUS_CHIP_HEIGHT, maxWidth: '100%', py: 0, whiteSpace: 'normal' }),
        '& .MuiChip-label': {
          px: 0,
          fontSize: FONT_SIZE.xs,
          fontWeight: FONT_WEIGHT.semibold,
          display: 'inline-flex',
          alignItems: wrap ? 'baseline' : 'center',
          gap: 0.5,
          overflow: 'visible',
          flexShrink: wrap ? 1 : 0,
          ...(wrap && { minWidth: 0, lineHeight: LINE_HEIGHT.snug, overflowWrap: 'anywhere' }),
        },
        ...(swap && PRESS_SWAP_SX),
      }}
    /></Tip>
  )
}

export default StatusChip
