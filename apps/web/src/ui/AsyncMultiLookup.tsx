import { useMemo, useRef, useState } from 'react'
import type { JSX, KeyboardEvent, MouseEvent, ReactNode } from 'react'
import Autocomplete from '@mui/material/Autocomplete'
import Box from '@mui/material/Box'
import IconButton from '@mui/material/IconButton'
import { optionText, useLookupField, type LookupFieldProps, type LookupOption } from './AsyncLookup'
import { ELLIPSIS_SX, TAP_FLOOR_DOWN } from './listStyles'
import { FONT_SIZE, LINE_HEIGHT, RADIUS, SIZE, grid } from '../theme'
import Tip from './Tip'

/* The server-backed lookup holding SEVERAL values, each a removable chip — `ui/AsyncLookup`'s many-valued twin.
 * Same option list, popper, rows and field (`AsyncLookup`'s `useLookupField`). Also the task
 * tag editor's box (`components/tasks/TaskTags.tsx`), whose read-only badges reuse `CHIP_SX`/`CHIP_TEXT_SX`.
 *
 * The caller holds the values WITH their labels: a chip never draws a raw key. A caller restoring stored keys
 * resolves their labels first (`pages/credentials/CredentialFilterStrip.tsx` asks its route by id).
 *
 * Enter on typed text (the Add-access dialog's DM1: type a name, Enter, done) resolves against the results for the
 * CURRENT text, awaiting the pending search (`useAsyncOptions`' `settle`) so a fast type+Enter picks. Exactly one
 * unpicked match is picked; several: the highlighted one only. `onEnterComplete` hears the pick, with the NEW value —
 * the caller's state may not have updated yet — and an empty box's Enter, with the values held, if any; without it
 * an empty box's Enter is MUI's own (the highlighted row).
 */

export interface AsyncMultiLookupProps extends Omit<LookupFieldProps, 'required'> {
  value: LookupOption[]
  onChange: (next: LookupOption[]) => void
  /** Shown only while nothing is picked; the chips say the rest. */
  placeholder?: string
  /** Names a chip's ✕ for assistive tech: `Remove <noun> <label · hint>` (the hint tells two same-named rows apart). */
  itemNoun?: string
  /** A chip's hover, always shown: for a chip whose label is a code that needs its name (`TaskTags`). Without
   *  it, the label · hint shows only on a cut chip. */
  chipTitle?: (option: LookupOption) => string
  /** A test id on each chip's TEXT, so `textContent` reads the label without the ✕. */
  chipTestId?: string
  /** Told the unambiguous pick Enter made (the header's DM1 rule), or the held values on an empty box. */
  onEnterComplete?: (next: LookupOption[]) => void
  /** Close the list after a pick — a click or an Enter — so it uncovers what sits below (a dialog's action row).
   *  Default false: the list stays open for several picks in a row. */
  closeOnSelect?: boolean
  /** A chip that cannot be removed here (a node a group carries): it draws read-only, no ✕, and this text is its
   *  hover, saying where it is removed. Its removal never reaches `onChange` (Backspace skips it too). */
  chipLocked?: (option: LookupOption) => string | null | undefined
}

/* A chip: the label in full, ellipsised only when it is wider than the field (`maxWidth: 100%`). */
export const CHIP_SX = {
  display: 'inline-flex',
  alignItems: 'center',
  /* The ✕ apart from the label, so "dev-node ✕" reads as a label and a control, not "dev-node×". */
  gap: 0.5,
  maxWidth: '100%',
  minWidth: 0,
  my: 0,
  mr: 0.5,
  pl: 1,
  border: '1px solid',
  borderColor: 'divider',
  borderRadius: RADIUS.sm,
  bgcolor: 'action.hover',
} as const
export const CHIP_TEXT_SX = { ...ELLIPSIS_SX, fontSize: FONT_SIZE.md, lineHeight: LINE_HEIGHT.normal, color: 'text.primary' } as const
/* The ✕: not the 44px phone floor inside a field, where it made every chip 44px tall, one per line. Pulled back to
   28px (above WCAG 2.5.8's 24px) in the theme's own media query; Backspace removes the last chip too. */
const CHIP_REMOVE_SX = {
  p: 0,
  pr: 0.5,
  fontSize: SIZE.iconXs,
  lineHeight: LINE_HEIGHT.tight,
  color: 'text.secondary',
  [TAP_FLOOR_DOWN]: { minHeight: SIZE.avatar, minWidth: SIZE.avatar },
} as const

/* The typing room after the chips: below it the input wraps to a line of its own, so three chips never squeeze
   the box to a sliver. */
const FIELD_SX = { '& .MuiAutocomplete-inputRoot .MuiAutocomplete-input': { minWidth: grid(16) } } as const

/** A chip's label, with the full label as its hover only when the chip is cut: a whole name needs no tooltip. */
function ChipText({ text, testId, children }: { text: string; testId?: string; children: ReactNode }): JSX.Element {
  const [cut, setCut] = useState(false)
  return (
    <Tip title={cut ? text : undefined}>
      <Box
        component="span"
        data-testid={testId}
        sx={CHIP_TEXT_SX}
        onMouseEnter={(event: MouseEvent<HTMLElement>) => setCut(event.currentTarget.scrollWidth > event.currentTarget.clientWidth)}
      >
        {children}
      </Box>
    </Tip>
  )
}

export function AsyncMultiLookup({
  value, onChange, placeholder, itemNoun = 'item', chipTitle, chipTestId, onEnterComplete, closeOnSelect = false, chipLocked, ...field
}: AsyncMultiLookupProps): JSX.Element {
  /* Here the box holds only typing: a pick clears it (MUI's `reset`), so there is no echo to tell apart. */
  const [inputValue, setInputValue] = useState('')
  /* Focused: the box's empty query is then a real one, so its debounce window reads busy, never "No matches". */
  const [active, setActive] = useState(false)
  /* Held here only for `closeOnSelect`: the Enter pick below stands MUI's own down, so MUI never closes it. */
  const [open, setOpen] = useState(false)
  /* MUI resets the typed text whenever `value` changes IDENTITY (`useAutocomplete`'s `value !== previous`), and a
     caller building chips per render hands a new array each time: held by content, so a re-render mid-typing
     keeps the text. */
  const valueKey = value.map(option => `${option.key}\u0000${option.label}`).join('\u0001')
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const stableValue = useMemo(() => value, [valueKey])

  const { scheduleQuery, requestFirstPage, settle, autocompleteProps, renderInput } = useLookupField(field, inputValue.trim(), active)

  /* DM1's bookkeeping: the text as of the awaited answer (a changed box discards it) and MUI's highlighted row. */
  const latestInput = useRef(inputValue)
  latestInput.current = inputValue
  const highlighted = useRef<LookupOption | null>(null)
  const onEnter = async (event: KeyboardEvent<HTMLDivElement> & { defaultMuiPrevented?: boolean }) => {
    if (event.key !== 'Enter' || event.nativeEvent.isComposing) return
    /* Only the text box's Enter: a chip's ✕ is a button inside the same root, and its Enter removes that
       chip — never a pick, never a report of the value the user is removing. */
    if (!(event.target instanceof HTMLInputElement)) return
    const query = inputValue.trim()
    /* An empty box with nothing to report: MUI's own Enter stands (it picks the row ↓ highlighted). */
    if (!query && !onEnterComplete) return
    /* MUI's own Enter pick is stood down; this handler is the one pick. The surrounding form never submits. */
    event.defaultMuiPrevented = true
    event.preventDefault()
    if (!query) {
      if (value.length) onEnterComplete?.(value)
      return
    }
    let rows: readonly LookupOption[]
    try {
      rows = await settle()
    } catch {
      return
    }
    if (latestInput.current.trim() !== query) return
    const candidates = rows.filter(option => !value.some(picked => picked.key === option.key))
    if (!candidates.length) return
    const unambiguous = candidates.length === 1
    const pick = unambiguous
      ? candidates[0]
      : (candidates.find(option => option.key === highlighted.current?.key) ?? candidates[0])
    const next = [...value, pick]
    onChange(next)
    setInputValue('')
    if (closeOnSelect) setOpen(false)
    scheduleQuery()
    if (unambiguous) onEnterComplete?.(next)
  }

  return (
    <Autocomplete<LookupOption, true, true, false>
      {...autocompleteProps}
      onKeyDown={onEnter}
      multiple
      value={stableValue}
      inputValue={inputValue}
      sx={FIELD_SX}
      /* A picked value leaves the list; removal is its ✕ or Backspace. No clear-all ✕: wiping every value has no undo. */
      filterSelectedOptions
      disableClearable
      /* Several picks in a row without reopening, unless the caller needs the list out of the way. */
      disableCloseOnSelect={!closeOnSelect}
      {...(closeOnSelect && {
        open,
        onOpen: () => {
          setOpen(true)
          autocompleteProps.onOpen()
        },
        onClose: () => setOpen(false),
      })}
      onChange={(_event, next) => {
        /* A locked chip stays: a removal that drops one (Backspace on the last chip) is no change at all. */
        if (chipLocked && value.some(option => chipLocked(option) && !next.some(kept => kept.key === option.key))) return
        onChange(next)
      }}
      onInputChange={(_event, next, reason) => {
        setInputValue(reason === 'input' ? next : '')
        // Also on a pick's reset: the cleared box asks for the first page again.
        scheduleQuery()
      }}
      onFocus={() => {
        setActive(true)
        requestFirstPage()
      }}
      onBlur={() => {
        setActive(false)
        autocompleteProps.onBlur()
      }}
      onHighlightChange={(_event, option) => {
        highlighted.current = option
      }}
      renderValue={(picked, getItemProps) =>
        picked.map((option, index) => {
          /* `key` out (React 19 spread-key warning), `disabled` out (invalid on a `<span>`); the rest is what
             MUI's arrow-key/Backspace handling addresses chips by. */
          const { key, onDelete, disabled: chipDisabled, ...itemProps } = getItemProps({ index })
          const locked = chipLocked?.(option)
          if (locked) {
            return (
              <Tip key={key} title={locked}><Box {...itemProps} component="span" data-testid="lookup-chip"
                data-locked="true" aria-description={locked} sx={{ ...CHIP_SX, pr: 1 }}>
                <Box component="span" data-testid={chipTestId} sx={CHIP_TEXT_SX}>{option.label}</Box>
              </Box></Tip>
            )
          }
          return (
            <Tip key={key} title={chipTitle?.(option)}><Box {...itemProps} component="span" data-testid="lookup-chip" sx={CHIP_SX}>
              {chipTitle
                ? <Box component="span" data-testid={chipTestId} sx={CHIP_TEXT_SX}>{option.label}</Box>
                : <ChipText text={optionText(option)} testId={chipTestId}>{option.label}</ChipText>}
              <IconButton
                type="button"
                aria-label={`Remove ${itemNoun} ${optionText(option)}`}
                disabled={chipDisabled}
                onClick={onDelete}
                sx={CHIP_REMOVE_SX}
              >
                <Box component="span" aria-hidden="true" sx={{ lineHeight: LINE_HEIGHT.tight }}>
                  ×
                </Box>
              </IconButton>
            </Box></Tip>
          )
        })
      }
      renderInput={params => renderInput(params, {
        placeholder: value.length === 0 ? placeholder : undefined,
        /* A placeholder is visible only with the label resting above the box, and chips need it there too. */
        shrink: Boolean(placeholder) || value.length > 0,
      })}
    />
  )
}

export default AsyncMultiLookup
