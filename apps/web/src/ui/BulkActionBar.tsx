import { useState, type JSX } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { countOf } from '../lib/humanize'
import FilterBar from './FilterBar'
import { PHONE } from './listStyles'
import { OverflowMenu } from './OverflowMenu'
import { GLYPH_BUTTON_SX } from './ToolbarMenu'
import { FONT_WEIGHT, RADIUS, SIZE, tint } from '../theme'
import Tip from './Tip'

/* The console's one bulk-action strip: count said out loud, one way to clear, destructive verbs look
 * destructive. A refused INLINE action is `aria-disabled` on a live button, never `disabled`: a disabled
 * button leaves the tab order and MUI sets `pointer-events: none`, hiding the `disabledReason`. Its
 * press is refused in `run`, which says why on a line under the strip — a `title` never shows on a tap. Every verb
 * keeps its word at every width (a bare ✓ beside ⊘ read as nothing on a phone): the verbs wrap rather
 * than overflow, each button keeps a 44px phone height, and extra actions collapse into a `ui/OverflowMenu`
 * pinned to the strip's end (never wrapped onto a second line); a lone inline verb (`maxInline` 1) never wraps either:
 * its words ellipsize, its full name staying its accessible name and tip. A refused ⋯-menu row carries its own reason,
 * printed under the verb and reachable by arrow key, so `run`'s line under the strip is the inline verbs'. */

/** One verb the bar offers. */
export type BulkAction = {
  /** `bulk-action-<id>` is built from this, inline and in the overflow menu alike. */
  id: string
  /** The word on the button, at every width, and its accessible name. */
  label: string
  /** The mark before the word. A text glyph, like every other icon in this repo. */
  icon?: string
  /* `error` + `outlined` (`listStyles.ts`'s `ROW_ACTION_SX` says why never `contained`). */
  destructive?: boolean
  disabled?: boolean
  /** Why it is unavailable. Without it a disabled action is a dead end. */
  disabledReason?: string
  onRun: () => void
}

/* Accent tint, not `action.selected` (a neutral wash). `tint()` (the palette's channel variable)
   repaints on theme flip where `alpha()` does not.
   Border as well as fill so the band is not a colour-only cue (WCAG 1.4.1). */
const BAR_SX = {
  px: 1,
  borderRadius: RADIUS.sm,
  border: '1px solid',
  borderColor: 'primary.main',
  bgcolor: tint('primary', 0.08),
} as const

/* The count never truncates: every verb acts on it. */
const COUNT_SX = {
  flex: '0 0 auto',
  fontWeight: FONT_WEIGHT.bold,
  color: 'text.primary',
  whiteSpace: 'nowrap',
} as const

/* A verb with its word: the 44px phone height, its natural width (the strip wraps). */
const ACTION_SX = { [PHONE]: { flex: '0 0 auto', minHeight: SIZE.tapTarget } } as const

/* `textTransform`: the word is a `variant="button"` Typography, which `typography.button` uppercases. */
const LABEL_SX = { textTransform: 'none' } as const

/* A LONE inline verb (`maxInline` 1) keeps the strip one line: it shrinks and ellipsizes its words rather than wrap
   under the count — its full name stays its accessible name and hover tip. */
const LONE_ACTION_SX = { minWidth: 0, flex: '0 1 auto', [PHONE]: { flex: '0 1 auto', minHeight: SIZE.tapTarget } } as const
const LONE_LABEL_SX = { ...LABEL_SX, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } as const

/* Clear is wordless at every width — the escape hatch must fit 390px; `aria-label`/`title` carry the words. */
const CLEAR_SX = {
  ...GLYPH_BUTTON_SX,
  color: 'text.secondary',
} as const

export default function BulkActionBar({
  count,
  onClear,
  actions,
  maxInline = 3,
  label = 'Bulk actions',
}: {
  /** How many rows are picked. At zero the bar renders nothing. */
  count: number
  /** Drop the selection. The bar's escape hatch, and the only control on it that is never disabled. */
  onClear: () => void
  /** The verbs, in the order they should be reached. */
  actions: BulkAction[]
  /* How many stay inline — nominated by position, not measured. Three fits beside the count and ✕ at
     390px with 44px buttons. */
  maxInline?: number
  /** The region's accessible name. Defaulted, because most lists have nothing more specific to say. */
  label?: string
}): JSX.Element | null {
  /* The refused verb last pressed, and the selection size it was pressed at: its reason shows while that
     selection stands and the verb stays refused, so a new selection never inherits an old refusal. */
  const [refusedAt, setRefusedAt] = useState<{ id: string; count: number } | null>(null)

  /* Nothing when nothing is picked: the bar's arrival is the selection signal. */
  if (count === 0) return null

  const inline = actions.slice(0, maxInline)
  const overflowed = actions.slice(maxInline)
  const lone = inline.length === 1 && maxInline === 1

  /* One press handler for button and menu item, so the ⋯ cannot run a refused verb. */
  const run = (action: BulkAction) => {
    if (action.disabled) {
      setRefusedAt({ id: action.id, count })
      return
    }
    setRefusedAt(null)
    action.onRun()
  }

  /** The verb's name, plus the reason while refused: the accessible name AND the hover `title`. */
  const actionName = (action: BulkAction) =>
    action.disabled && action.disabledReason ? `${action.label} — ${action.disabledReason}` : action.label

  const refused = refusedAt?.count === count
    ? actions.find(a => a.id === refusedAt.id && a.disabled && a.disabledReason)
    : undefined

  return (
    /* A named region (landmark). The count is `role="status"` so growth is announced while inside. */
    <Box role="region" aria-label={label} data-testid="bulk-action-bar" sx={BAR_SX}>
      <FilterBar>
        {/* One child: `FilterBar` stacks children in a column below `sm`, wrong for a row of tap targets. Inside it,
            the verbs wrap and the ⋯ keeps the row's end — in the wrapping Stack it landed alone on a second line. */}
        <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 1, minWidth: 0, width: '100%' }}>
          <Stack direction="row" sx={{ alignItems: 'center', gap: 1, flexWrap: lone ? 'nowrap' : 'wrap', minWidth: 0, flex: '1 1 auto' }}>
            <Typography
              component="span"
              variant="body2"
              role="status"
              data-testid="bulk-action-bar-count"
              sx={COUNT_SX}
            >
              {count} selected
            </Typography>

            <Button
              variant="text"
              color="inherit"
              onClick={onClear}
              aria-label={`Clear the selection of ${countOf(count, 'row')}`}
              data-testid="bulk-action-bar-clear"
              sx={CLEAR_SX}
            >
              <Box component="span" aria-hidden="true">✕</Box>
            </Button>

            {inline.map(action => (
              <Tip key={action.id} title={actionName(action)}><Button
                variant="outlined"
                color={action.destructive ? 'error' : 'inherit'}
                onClick={() => run(action)}
                /* Not `disabled` — see the file header; `run` makes the press a no-op. */
                aria-disabled={action.disabled || undefined}
                aria-label={actionName(action)}
                data-testid={`bulk-action-${action.id}`}
                sx={{
                  ...(lone ? LONE_ACTION_SX : ACTION_SX),
                  /* The theme's disabled face by hand, since the element is not disabled. */
                  ...(action.disabled ? { color: 'text.disabled', borderColor: 'action.disabledBackground' } : {}),
                }}
              >
                {action.icon && <Box component="span" aria-hidden="true">{action.icon}</Box>}
                <Typography component="span" variant="button" sx={lone ? LONE_LABEL_SX : LABEL_SX}>
                  {action.icon ? <>&nbsp;{action.label}</> : action.label}
                </Typography>
              </Button></Tip>
            ))}
          </Stack>

          <OverflowMenu
            ariaLabel={`More ${label.toLowerCase()}`}
            sx={{ ...ACTION_SX, ml: 'auto' }}
            items={overflowed.map(action => ({
              key: action.id,
              testId: `bulk-action-${action.id}`,
              icon: action.icon,
              label: action.label,
              ariaLabel: action.label,
              danger: action.destructive,
              disabled: action.disabled,
              disabledReason: action.disabledReason,
              onClick: () => run(action),
            }))}
          />
        </Box>
      </FilterBar>
      {/* Mounted empty so a screen reader announces the reason when it lands. */}
      <Typography variant="caption" component="div" role="status" sx={{ color: 'text.secondary', pb: refused ? 0.75 : 0 }}>
        {refused && <span data-testid="bulk-action-refused">{refused.label}: {refused.disabledReason}</span>}
      </Typography>
    </Box>
  )
}
