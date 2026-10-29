import type { JSX, ReactNode } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'

/* ONE PICKABLE OPTION — a native radio inside a label, so a group of them is one tab stop with arrow-key moves
 * and a real checked state for assistive tech, with no keyboard code of our own.
 *
 * Two renderings over the same markup. `card`: a bordered tile for a grid. `row`: one line of a vertical list —
 * a drawn radio glyph, the title, the price aside — with the body beneath, which a caller passes only for the
 * row it wants opened (the wizard's selected way, the hosting picker's picked kind).
 *
 * THE TITLE WRAPS, NEVER ELLIPSIZES (AC14.3): a cut title is a choice nobody can read. Cards in a grid fill
 * their cell (`height: 100%`), so a row is as tall as its tallest card. A disabled option stays legible — its
 * reason is the point — and says so in words, never by fading alone.
 *
 * THE WHOLE CARD IS THE RADIO: the native input is stretched transparent over the card, so a click anywhere on
 * it lands on the radio itself — nothing covers it, and no stray pixel of the card misses the pick. */
const RADIO_OVERLAY = {
  position: 'absolute',
  inset: 0,
  width: '100%',
  height: '100%',
  margin: 0,
  opacity: 0,
  cursor: 'inherit',
  zIndex: 1,
} as const

export function ChoiceCard({
  name,
  value,
  checked,
  disabled = false,
  onSelect,
  title,
  aside,
  badge,
  children,
  testId,
  variant = 'card',
}: {
  /** The radio group's name: one per group. */
  name: string
  value: string
  checked: boolean
  disabled?: boolean
  onSelect: (value: string) => void
  title: ReactNode
  /** Right of the title: a price, aligned with its neighbours' (tabular numerals). */
  aside?: ReactNode
  /** Beside the title (row) or under it (card): "Recommended". */
  badge?: ReactNode
  children?: ReactNode
  testId?: string
  variant?: 'card' | 'row'
}): JSX.Element {
  const row = variant === 'row'
  const textColor = disabled ? 'text.secondary' : 'text.primary'
  return (
    <Box
      component="label"
      data-testid={testId}
      data-checked={checked ? 'true' : undefined}
      data-disabled={disabled ? 'true' : undefined}
      sx={{
        position: 'relative',
        display: 'flex',
        flexDirection: row ? 'row' : 'column',
        alignItems: row ? 'flex-start' : undefined,
        gap: row ? 1.5 : 0.75,
        height: row ? undefined : '100%',
        boxSizing: 'border-box',
        p: row ? 1.25 : 2,
        minWidth: 0,
        borderRadius: row ? 1 : 2,
        ...(row ? {} : {
          border: 1,
          borderColor: checked ? 'primary.main' : 'divider',
          // The ring doubles the border without moving the content a pixel.
          boxShadow: theme => (checked ? `inset 0 0 0 1px ${theme.palette.primary.main}` : 'none'),
        }),
        // A primary tint in the channel form: `alpha(theme.palette.…)` cannot parse a CSS variable and freezes at light.
        bgcolor: disabled && !row ? 'action.hover' : checked ? 'rgba(var(--mui-palette-primary-mainChannel) / 0.08)' : 'background.paper',
        cursor: disabled ? 'not-allowed' : 'pointer',
        transition: 'border-color 120ms, background-color 120ms',
        '&:hover': disabled || checked ? {} : row ? { bgcolor: 'action.hover' } : { borderColor: 'text.secondary' },
        '&:has(input:focus-visible)': { outline: 2, outlineStyle: 'solid', outlineColor: 'primary.main', outlineOffset: 2 },
      }}
    >
      <input
        type="radio"
        name={name}
        value={value}
        checked={checked}
        disabled={disabled}
        onChange={() => onSelect(value)}
        style={RADIO_OVERLAY}
      />
      {row && (
        /* The glyph: drawn, so the native radio can stay hidden and keep its accessibility. */
        <Box
          aria-hidden="true"
          sx={{
            flexShrink: 0,
            mt: '3px',
            width: 18,
            height: 18,
            borderRadius: '50%',
            boxSizing: 'border-box',
            border: 2,
            // Dashed when disabled: a ring that cannot be filled, beside the words that say why.
            borderStyle: disabled ? 'dashed' : 'solid',
            borderColor: disabled ? 'text.disabled' : checked ? 'primary.main' : 'text.secondary',
            display: 'grid',
            placeItems: 'center',
            '&::after': {
              content: '""',
              width: 8,
              height: 8,
              borderRadius: '50%',
              bgcolor: checked ? 'primary.main' : 'transparent',
            },
          }}
        />
      )}
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.75, minWidth: 0, flex: '1 1 auto' }}>
        <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 1.5, flexWrap: row ? 'wrap' : undefined }}>
          <Typography
            component="span"
            sx={{
              flex: row ? '0 1 auto' : '1 1 auto',
              minWidth: 0,
              fontWeight: 600,
              fontSize: '0.9375rem',
              lineHeight: 1.35,
              overflowWrap: 'anywhere',
              color: textColor,
            }}
          >
            {title}
          </Typography>
          {row && badge}
          {aside !== undefined && (
            <Box
              component="span"
              sx={{
                flexShrink: 0,
                ml: row ? 'auto' : undefined,
                textAlign: 'right',
                fontVariantNumeric: 'tabular-nums',
                fontWeight: 600,
                whiteSpace: 'nowrap',
                color: textColor,
              }}
            >
              {aside}
            </Box>
          )}
        </Box>
        {!row && badge}
        {children && (
          <Box sx={{ color: 'text.secondary', fontSize: '0.875rem', lineHeight: 1.45 }}>{children}</Box>
        )}
      </Box>
    </Box>
  )
}

/** The grid ChoiceCards sit in: one column on a phone, each card its own height; two from 600px, every row as
 *  tall as its tallest card. */
export const CHOICE_GRID_SX = {
  display: 'grid',
  gridTemplateColumns: { xs: 'minmax(0, 1fr)', sm: 'repeat(2, minmax(0, 1fr))' },
  gridAutoRows: { xs: 'auto', sm: '1fr' },
  gap: 1.5,
} as const

/** The vertical list `variant="row"` options sit in: one option per line, no columns at any width. */
export const CHOICE_LIST_SX = {
  display: 'flex',
  flexDirection: 'column',
  gap: 0.25,
} as const

export default ChoiceCard
