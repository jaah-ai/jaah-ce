import { useState, type JSX, type ReactNode } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import MenuItem from '@mui/material/MenuItem'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import FilterBar from './FilterBar'
import { PHONE } from './listStyles'
import { GLYPH_BUTTON_SX } from './ToolbarMenu'

/* The console's one bulk-action strip: count said out loud, one way to clear, destructive verbs look
 * destructive. A refused action is `aria-disabled` on a live button, never `disabled`: a disabled
 * button leaves the tab order and MUI sets `pointer-events: none`, hiding the `disabledReason`. The
 * press is refused in `run`, which says why on a line under the strip — a `title` never shows on a tap. Every verb
 * keeps its word at every width (a bare ✓ beside ⊘ read as nothing on a phone): the strip wraps rather
 * than overflows, each button keeps a 44px phone height, and extra actions collapse into `FilterBar`'s ⋯ menu. */

/** One verb the bar offers. */
export type BulkAction = {
  /** `bulk-action-<id>` is built from this, inline and in the overflow menu alike. */
  id: string
  /** The word on the button, at every width, and its accessible name. */
  label: string
  /** The mark before the word. A text glyph, like every other icon in this repo. */
  icon?: ReactNode
  /* `error` + `outlined` (`listStyles.ts`'s `ROW_ACTION_SX` says why never `contained`). */
  destructive?: boolean
  disabled?: boolean
  /** Why it is unavailable. Without it a disabled action is a dead end. */
  disabledReason?: string
  onRun: () => void
}

/* Accent tint, not `action.selected` (a neutral wash). The `rgba(var(--mui-palette-…-mainChannel) / …)`
   spelling repaints on theme flip where `alpha()` does not.
   Border as well as fill so the band is not a colour-only cue (WCAG 1.4.1). */
const BAR_SX = {
  px: 1,
  borderRadius: 1,
  border: '1px solid',
  borderColor: 'primary.main',
  bgcolor: 'rgba(var(--mui-palette-primary-mainChannel) / 0.08)',
} as const

/* The count never truncates: every verb acts on it. */
const COUNT_SX = {
  flex: '0 0 auto',
  fontWeight: 700,
  color: 'text.primary',
  whiteSpace: 'nowrap',
} as const

/* A verb with its word: the 44px phone height, its natural width (the strip wraps). */
const ACTION_SX = { [PHONE]: { flex: '0 0 auto', minHeight: 44 } } as const

/* `textTransform`: the word is a `variant="button"` Typography, which `typography.button` uppercases. */
const LABEL_SX = { textTransform: 'none' } as const

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
  /* How many stay inline — nominated by position (`FilterBar`'s overflow contract), not measured.
     Three fits beside the count and ✕ at 390px with 44px buttons. */
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

  /* One press handler for button and menu item, so the ⋯ cannot run a refused verb. */
  const run = (action: BulkAction, close?: () => void) => {
    if (action.disabled) {
      setRefusedAt({ id: action.id, count })
      close?.()
      return
    }
    setRefusedAt(null)
    close?.()
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
      <FilterBar
        overflowLabel={`More ${label.toLowerCase()}`}
        overflow={
          overflowed.length === 0
            ? undefined
            : close =>
                overflowed.map(action => (
                  <MenuItem
                    key={action.id}
                    onClick={() => run(action, close)}
                    /* Live-but-refused, as with the buttons: a `disabled` MenuItem is unfocusable. */
                    aria-disabled={action.disabled || undefined}
                    title={action.disabled ? action.disabledReason : undefined}
                    aria-label={actionName(action)}
                    data-testid={`bulk-action-${action.id}`}
                    sx={{
                      gap: 1,
                      color: action.destructive ? 'error.main' : 'text.primary',
                      ...(action.disabled ? { opacity: 0.5 } : {}),
                    }}
                  >
                    {action.icon && <Box component="span" aria-hidden="true">{action.icon}</Box>}
                    {action.label}
                  </MenuItem>
                ))
        }
      >
        {/* One child: `FilterBar` stacks children in a column below `sm`, wrong for a row of tap targets. */}
        <Stack direction="row" sx={{ alignItems: 'center', gap: 1, flexWrap: 'wrap', minWidth: 0 }}>
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
            aria-label={`Clear the selection of ${count} ${count === 1 ? 'row' : 'rows'}`}
            title="Clear the selection"
            data-testid="bulk-action-bar-clear"
            sx={CLEAR_SX}
          >
            <Box component="span" aria-hidden="true">✕</Box>
          </Button>

          {inline.map(action => (
            <Button
              key={action.id}
              variant="outlined"
              color={action.destructive ? 'error' : 'inherit'}
              onClick={() => run(action)}
              /* Not `disabled` — see the file header; `run` makes the press a no-op. */
              aria-disabled={action.disabled || undefined}
              title={actionName(action)}
              aria-label={actionName(action)}
              data-testid={`bulk-action-${action.id}`}
              sx={{
                ...ACTION_SX,
                /* The theme's disabled face by hand, since the element is not disabled. */
                ...(action.disabled ? { color: 'text.disabled', borderColor: 'action.disabledBackground' } : {}),
              }}
            >
              {action.icon && <Box component="span" aria-hidden="true">{action.icon}</Box>}
              <Typography component="span" variant="button" sx={LABEL_SX}>
                {action.icon ? <>&nbsp;{action.label}</> : action.label}
              </Typography>
            </Button>
          ))}
        </Stack>
      </FilterBar>
      {/* Mounted empty so a screen reader announces the reason when it lands. */}
      <Typography variant="caption" component="div" role="status" sx={{ color: 'text.secondary', pb: refused ? 0.75 : 0 }}>
        {refused && <span data-testid="bulk-action-refused">{refused.label}: {refused.disabledReason}</span>}
      </Typography>
    </Box>
  )
}
