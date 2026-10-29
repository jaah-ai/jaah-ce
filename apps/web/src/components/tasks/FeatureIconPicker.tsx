import { type KeyboardEvent, useRef, useState } from 'react'
import Box from '@mui/material/Box'
import ButtonBase from '@mui/material/ButtonBase'
import Popover from '@mui/material/Popover'
import Typography from '@mui/material/Typography'
import type { Theme } from '@mui/material/styles'
import { FEATURE_ICON_COLOURS, FEATURE_ICON_SHAPES, FEATURE_ICONS, parseFeatureIcon } from '../../lib/featureIcon'
import FeatureIcon from './FeatureIcon'
import { capitalize } from '../../lib/humanize'

/* The Feature Edit page's icon control (`pages/FeatureEditPage.tsx`): a button showing the current icon — or
 * a dashed empty slot named "Choose icon" when there is none — that opens a palette. The palette is ONE
 * `radiogroup`: "No icon", then the 24 tokens shape-major (3 rows × 8 colours, `FEATURE_ICONS`' order).
 * Arrow keys move focus (roving tabindex), Enter/Space or a click picks and closes. The pick is the
 * caller's state: this component persists nothing. */

const COLUMNS = FEATURE_ICON_COLOURS.length
// Item 0 is "No icon"; item i (1..24) is FEATURE_ICONS[i - 1].
const ITEMS: readonly (string | null)[] = [null, ...FEATURE_ICONS]

/** "Red triangle", or "No icon". */
export function featureIconLabel(icon: string | null | undefined): string {
  const parsed = parseFeatureIcon(icon)
  return parsed ? `${capitalize(parsed.colour)} ${parsed.shape}` : 'No icon'
}

/* Where an arrow key lands. Left/Right walk the list; Up/Down move a row within the grid, and the row above
 * the grid's first row is "No icon". Clamped, not wrapped: an edge stays put. */
function nextIndex(from: number, key: string): number | null {
  const last = ITEMS.length - 1
  switch (key) {
    case 'ArrowRight':
      return Math.min(from + 1, last)
    case 'ArrowLeft':
      return Math.max(from - 1, 0)
    case 'ArrowDown':
      return from === 0 ? 1 : Math.min(from + COLUMNS, last)
    case 'ArrowUp':
      return from <= COLUMNS ? 0 : from - COLUMNS
    case 'Home':
      return 0
    case 'End':
      return last
    default:
      return null
  }
}

const CELL = 32

export default function FeatureIconPicker({
  value,
  onChange,
  disabled,
  describedBy,
}: {
  value: string | null
  onChange: (icon: string | null) => void
  disabled?: boolean
  /** The id of an error naming this control (the page's server 422 for `icon`). */
  describedBy?: string
}) {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null)
  const current = parseFeatureIcon(value) ? value : null
  const selectedIndex = ITEMS.indexOf(current)
  const [focusIndex, setFocusIndex] = useState(selectedIndex)
  const cells = useRef<(HTMLButtonElement | null)[]>([])

  function openPalette(el: HTMLElement) {
    setFocusIndex(selectedIndex)
    setAnchor(el)
  }

  function pick(index: number) {
    onChange(ITEMS[index])
    setAnchor(null)
  }

  function onGridKeyDown(e: KeyboardEvent<HTMLElement>) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      pick(focusIndex)
      return
    }
    const next = nextIndex(focusIndex, e.key)
    if (next === null) return
    e.preventDefault()
    setFocusIndex(next)
    cells.current[next]?.focus()
  }

  function cell(index: number) {
    const icon = ITEMS[index]
    const checked = index === selectedIndex
    return (
      <ButtonBase
        key={icon ?? 'none'}
        ref={el => {
          cells.current[index] = el
        }}
        role="radio"
        aria-checked={checked}
        aria-label={featureIconLabel(icon)}
        title={featureIconLabel(icon)}
        tabIndex={index === focusIndex ? 0 : -1}
        data-testid={`feature-icon-option-${icon ?? 'none'}`}
        onClick={() => pick(index)}
        onFocus={() => setFocusIndex(index)}
        sx={(theme: Theme) => ({
          minWidth: CELL,
          height: CELL,
          px: icon ? 0 : 1,
          borderRadius: 1,
          border: 2,
          borderColor: checked ? 'primary.main' : 'transparent',
          gap: 0.75,
          '&:hover': { bgcolor: 'action.hover' },
          '&.Mui-focusVisible': { outline: `2px solid ${theme.palette.primary.main}`, outlineOffset: 1 },
        })}
      >
        {icon ? (
          <FeatureIcon icon={icon} size={12} testId="feature-icon-option-glyph" />
        ) : (
          <Typography variant="body2" component="span">No icon</Typography>
        )}
      </ButtonBase>
    )
  }

  return (
    <>
      <ButtonBase
        aria-label={current ? `Icon: ${featureIconLabel(current)}. Change icon` : 'Choose icon'}
        aria-haspopup="dialog"
        aria-expanded={anchor !== null}
        aria-describedby={describedBy}
        data-testid="feature-icon-button"
        disabled={disabled}
        onClick={e => openPalette(e.currentTarget)}
        sx={(theme: Theme) => ({
          flex: '0 0 auto',
          width: 40,
          height: 40,
          borderRadius: 1,
          border: 1,
          borderStyle: current ? 'solid' : 'dashed',
          borderColor: current ? 'divider' : 'text.disabled',
          '&:hover': { bgcolor: 'action.hover' },
          '&.Mui-focusVisible': { outline: `2px solid ${theme.palette.primary.main}`, outlineOffset: 1 },
        })}
      >
        {current && <FeatureIcon icon={current} size={16} testId="feature-icon-preview" />}
      </ButtonBase>
      <Popover
        open={anchor !== null}
        anchorEl={anchor}
        onClose={() => setAnchor(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'left' }}
        slotProps={{
          paper: { role: 'dialog', 'aria-label': 'Choose icon', sx: { p: 1 } } as object,
          transition: { onEntered: () => cells.current[focusIndex]?.focus() },
        }}
      >
        <Box
          role="radiogroup"
          aria-label="Feature icon"
          data-testid="feature-icon-palette"
          onKeyDown={onGridKeyDown}
          sx={{ display: 'flex', flexDirection: 'column', gap: 0.5 }}
        >
          <Box>{cell(0)}</Box>
          {FEATURE_ICON_SHAPES.map((shape, row) => (
            <Box key={shape} sx={{ display: 'grid', gridTemplateColumns: `repeat(${COLUMNS}, ${CELL}px)`, gap: 0.5 }}>
              {FEATURE_ICON_COLOURS.map((_, col) => cell(1 + row * COLUMNS + col))}
            </Box>
          ))}
        </Box>
      </Popover>
    </>
  )
}
