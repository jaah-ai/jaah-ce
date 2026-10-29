import { useMemo, useState } from 'react'
import type { JSX, MouseEvent, ReactNode } from 'react'
import Autocomplete from '@mui/material/Autocomplete'
import Box from '@mui/material/Box'
import IconButton from '@mui/material/IconButton'
import { optionText, renderLookupInput, useLookupField, type LookupOption } from './AsyncLookup'
import { ELLIPSIS_SX, TAP_FLOOR_DOWN } from './listStyles'

/* The server-backed lookup holding SEVERAL values, each a removable chip — `ui/AsyncLookup`'s many-valued twin.
 * Same option list, popper, rows and field (`AsyncLookup`'s `useLookupField` + `renderLookupInput`). The chip
 * pattern is `components/tasks/TaskTags.tsx`'s badge.
 *
 * The caller holds the values WITH their labels: a chip never draws a raw key. A caller restoring stored keys
 * resolves their labels first (`pages/credentials/CredentialFilterStrip.tsx` asks its route by id).
 */

export interface AsyncMultiLookupProps {
  /** Required and stable — it is the input's `id`, so a `<label htmlFor>` can reach it. */
  id: string
  value: LookupOption[]
  onChange: (next: LookupOption[]) => void
  /** As `AsyncLookup`'s: passes the trimmed query and the `AbortSignal` to the server. */
  fetchOptions: (query: string, signal: AbortSignal) => Promise<LookupOption[]>
  label?: string
  /** Shown only while nothing is picked; the chips say the rest. */
  placeholder?: string
  /** WCAG 2.5.3 (Label in Name) — the accessible name must CONTAIN the visible copy. */
  ariaLabel?: string
  /** Names a chip's ✕ for assistive tech: `Remove <noun> <label>`. */
  itemNoun?: string
  disabled?: boolean
  minChars?: number
  noOptionsText?: string
  fetchOnFocus?: boolean
  testId?: string
  error?: boolean
  helperText?: ReactNode
  fullWidth?: boolean
  size?: 'small' | 'medium'
  margin?: 'none' | 'dense'
  stacked?: boolean
}

/* A chip: the label in full, ellipsised only when it is wider than the field (`maxWidth: 100%`). */
const CHIP_SX = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: '0.125rem',
  maxWidth: '100%',
  minWidth: 0,
  my: '2px',
  mr: '4px',
  pl: '6px',
  border: '1px solid',
  borderColor: 'divider',
  borderRadius: 1,
  bgcolor: 'action.hover',
} as const
const CHIP_TEXT_SX = { ...ELLIPSIS_SX, fontSize: '0.8125rem', lineHeight: 1.6, color: 'text.primary' } as const
/* The ✕: not the 44px phone floor inside a field (`TaskTags`' reason): the theme's raise made every chip 44px tall,
   one per line. Pulled back to 28px (above WCAG 2.5.8's 24px) in the theme's own media query; Backspace removes
   the last chip too. */
const CHIP_REMOVE_SX = {
  p: '2px',
  fontSize: '0.875rem',
  lineHeight: 1,
  color: 'text.secondary',
  [TAP_FLOOR_DOWN]: { minHeight: 28, minWidth: 28 },
} as const

/* The typing room after the chips: below it the input wraps to a line of its own, so three chips never squeeze
   the box to a sliver. */
const FIELD_SX = { '& .MuiAutocomplete-inputRoot .MuiAutocomplete-input': { minWidth: '6rem' } } as const

/** The full label as the chip's hover only when the chip is cut: a whole name needs no tooltip. */
function titleIfCut(event: MouseEvent<HTMLElement>, text: string) {
  const el = event.currentTarget
  el.title = el.scrollWidth > el.clientWidth ? text : ''
}

export function AsyncMultiLookup({
  id,
  value,
  onChange,
  fetchOptions,
  label,
  placeholder,
  ariaLabel,
  itemNoun = 'item',
  disabled = false,
  minChars = 1,
  noOptionsText = 'No matches',
  fetchOnFocus = false,
  testId,
  error,
  helperText,
  fullWidth = true,
  size = 'small',
  margin,
  stacked = false,
}: AsyncMultiLookupProps): JSX.Element {
  /* Here the box holds only typing: a pick clears it (MUI's `reset`), so there is no echo to tell apart. */
  const [inputValue, setInputValue] = useState('')
  /* Focused: the box's empty query is then a real one, so its debounce window reads busy, never "No matches". */
  const [active, setActive] = useState(false)
  /* MUI resets the typed text whenever `value` changes IDENTITY (`useAutocomplete`'s `value !== previous`), and a
     caller building chips per render hands a new array each time: held by content, so a re-render mid-typing
     keeps the text. */
  const valueKey = value.map(option => `${option.key}\u0000${option.label}`).join('\u0001')
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const stableValue = useMemo(() => value, [valueKey])

  const { busy, scheduleQuery, requestFirstPage, autocompleteProps } = useLookupField({
    id, testId, fetchOptions, query: inputValue.trim(), typed: active, minChars, fetchOnFocus, noOptionsText,
    disabled, fullWidth, size, stacked,
  })

  return (
    <Autocomplete<LookupOption, true, true, false>
      {...autocompleteProps}
      multiple
      value={stableValue}
      inputValue={inputValue}
      sx={FIELD_SX}
      /* A picked value leaves the list; removal is its ✕ or Backspace. No clear-all ✕ (`TaskTags`' rule). */
      filterSelectedOptions
      disableClearable
      /* Several picks in a row without reopening. */
      disableCloseOnSelect
      onChange={(_event, next) => onChange(next)}
      onInputChange={(_event, next, reason) => {
        setInputValue(reason === 'input' ? next : '')
        // Also on a pick's reset: the cleared box asks for the first page again.
        scheduleQuery()
      }}
      onFocus={() => {
        setActive(true)
        requestFirstPage()
      }}
      onBlur={() => setActive(false)}
      renderValue={(picked, getItemProps) =>
        picked.map((option, index) => {
          /* `key` out (React 19 spread-key warning), `disabled` out (invalid on a `<span>`); the rest is what
             MUI's arrow-key/Backspace handling addresses chips by. */
          const { key, onDelete, disabled: chipDisabled, ...itemProps } = getItemProps({ index })
          return (
            <Box key={key} {...itemProps} component="span" data-testid="lookup-chip" sx={CHIP_SX}>
              <Box component="span" sx={CHIP_TEXT_SX} onMouseEnter={event => titleIfCut(event, optionText(option))}>
                {option.label}
              </Box>
              <IconButton
                type="button"
                aria-label={`Remove ${itemNoun} ${option.label}`}
                disabled={chipDisabled}
                onClick={onDelete}
                sx={CHIP_REMOVE_SX}
              >
                <Box component="span" aria-hidden="true" sx={{ lineHeight: 1 }}>
                  ×
                </Box>
              </IconButton>
            </Box>
          )
        })
      }
      renderInput={params => renderLookupInput(params, {
        busy, margin, label, error, helperText, ariaLabel,
        placeholder: value.length === 0 ? placeholder : undefined,
        /* A placeholder is visible only with the label resting above the box, and chips need it there too. */
        shrink: Boolean(placeholder) || value.length > 0,
      })}
    />
  )
}

export default AsyncMultiLookup
