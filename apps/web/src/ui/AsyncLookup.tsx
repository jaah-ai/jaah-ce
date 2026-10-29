import { useEffect, useRef, useState } from 'react'
import type { HTMLAttributes, JSX, Key, KeyboardEvent, ReactNode } from 'react'
import Autocomplete from '@mui/material/Autocomplete'
import type { AutocompleteRenderInputParams, AutocompleteRenderOptionState } from '@mui/material/Autocomplete'
import Box from '@mui/material/Box'
import CircularProgress from '@mui/material/CircularProgress'
import TextField from '@mui/material/TextField'
import { useAsyncOptions } from '../hooks/useAsyncOptions'
import { ELLIPSIS_SX, fieldTapTargetSx } from './listStyles'
import { NO_AUTOFILL_INPUT } from './noAutofill'

/* The console's ONE server-backed lookup: type, the server answers, take a row.
 *
 * vs `ui/FilterRow`'s `FilterSelect` (a static vocabulary → native `<select>`);
 * `components/tasks/TaskTags.tsx` stays separate (multi-select over a catalogue fetched once).
 *
 * Owns the box: the echo-vs-typed distinction and an Enter that never submits the surrounding form. The option
 * list (one in-flight request, superseded responses DISCARDED, honest loading and error lines) is
 * `hooks/useAsyncOptions`. Everything the two lookups share — that list, the popper's width, the rows, the
 * field — is `useLookupField` + `renderLookupInput` below, also `ui/AsyncMultiLookup`'s (several values, as chips).
 */

/** One offerable row. `hint` is the disambiguator (a project, a status, a kind); `key` is identity. */
export type LookupOption = {
  key: string
  label: string
  hint?: string
  disabled?: boolean
  /** A DECORATIVE glyph in a fixed square before the label (a vendor mark from `ui/credentialMarks`).
   *  Optional; absent renders the row unchanged. Never carries a fact: the slot is `aria-hidden`, so
   *  anything the operator must know belongs in label or hint. */
  icon?: ReactNode
}

export interface AsyncLookupProps {
  /** Required and stable — it is the input's `id`, so a `<label htmlFor>` can reach it. */
  id: string
  value: LookupOption | null
  onChange: (next: LookupOption | null) => void
  /* The only server seam: given the trimmed query and an `AbortSignal`, which it must pass to
     `api(path, { signal })`. Dropping it still renders correctly (superseded responses are discarded) but
     wastes requests. */
  fetchOptions: (query: string, signal: AbortSignal) => Promise<LookupOption[]>
  /** The visible field copy. Injected by `ui/FormField`, which lists `Autocomplete` as a valid child. */
  label?: string
  placeholder?: string
  /** WCAG 2.5.3 (Label in Name) — the accessible name must CONTAIN the visible copy. */
  ariaLabel?: string
  disabled?: boolean
  /** Below this many characters no request is made and the popup says so. The EMPTY query is governed
   *  by `fetchOnFocus` alone, never by this. */
  minChars?: number
  /** What an EMPTY answer to a real query reads as. A FAILED fetch never borrows this line. */
  noOptionsText?: string
  /** Treat the empty query as fetchable, so focusing the field offers a first page. */
  fetchOnFocus?: boolean
  testId?: string
  /* `ui/FormField`'s cloned contract: the single source of invalid state, so `error`/`helperText` are
     forwarded verbatim. */
  required?: boolean
  error?: boolean
  helperText?: ReactNode
  fullWidth?: boolean
  size?: 'small' | 'medium'
  /** `none` for a filter strip, whose fields cancel the theme's `margin: 'dense'` (`ui/FilterRow`): kept, the lookup
   *  sat 8px lower and 12px taller than the fields beside it. A form keeps the theme's. */
  margin?: 'none' | 'dense'
  /** Each row as two lines, the hint under the label at full width: for a hint that must stay readable on
   *  a phone (a person's email), where the side-by-side row caps it at 40%. */
  stacked?: boolean
  /** Draw no clear ✕: for a required choice no caller can unset (`ui/OrgSelect`), where the ✕ offers an act that
   *  does nothing. */
  disableClearable?: boolean
}

/* The debounce's numbers, re-exported from their home (`hooks/useAsyncOptions`) for the boxes that key on them. */
export { QUERY_DEBOUNCE_MS, QUERY_MAX_WAIT_MS } from '../hooks/useAsyncOptions'

/* A suggestion row: label left, hint dim on the right edge. On the INNER box, not the `<li>`: MUI's
   `.MuiAutocomplete-listbox .MuiAutocomplete-option` outranks a one-class `sx` (measured in `TaskTags`, which
   shares it). */
export const OPTION_ROW_SX = {
  display: 'flex',
  width: '100%',
  minWidth: 0,
  alignItems: 'baseline',
  justifyContent: 'space-between',
  gap: '0.75rem',
} as const

/* The label is the row: the hint is capped at 40%, and the
   label ellipsises with the full string on the row's `title`. */
const OPTION_LABEL_SX = { ...ELLIPSIS_SX, flex: '1 1 auto', fontSize: '0.8125rem', color: 'text.primary' } as const
const OPTION_HINT_SX = {
  ...ELLIPSIS_SX,
  flex: '0 1 auto',
  maxWidth: '40%',
  fontSize: '0.75rem',
  color: 'text.secondary',
} as const

/* `stacked`: label over hint, each ellipsising at the row's full width. */
const STACKED_ROW_SX = { display: 'flex', flexDirection: 'column', width: '100%', minWidth: 0 } as const
const STACKED_HINT_SX = { ...OPTION_HINT_SX, maxWidth: '100%' } as const

/* The icon slot: a FIXED square so glyphs form a column; `flexShrink: 0` so the label gives way;
   `alignSelf: 'center'` (an svg has no useful baseline). `aria-hidden` here, since the slot is decorative
   by contract. Rendered only when a row supplies an icon. */
const OPTION_ICON_SX = {
  flex: '0 0 auto',
  alignSelf: 'center',
  display: 'flex',
  alignItems: 'center',
  color: 'text.secondary',
} as const

/** A row's whole text, label then hint: the hover on a suggestion row and on the closed field. */
export function optionText(option: LookupOption): string {
  return option.hint ? `${option.label} · ${option.hint}` : option.label
}

/** The typed run, emboldened inside its label; plain when the server matched on an unrendered field. Also
 *  `TaskTags`'s, where the hit may be on the unrendered NAME. */
export function highlight(text: string, query: string): ReactNode {
  const needle = query.trim().toLowerCase()
  if (!needle) return text
  const at = text.toLowerCase().indexOf(needle)
  if (at < 0) return text
  return (
    <>
      {text.slice(0, at)}
      <Box component="strong" sx={{ fontWeight: 700 }}>
        {text.slice(at, at + needle.length)}
      </Box>
      {text.slice(at + needle.length)}
    </>
  )
}

/** The popper's inline style: at least the field's measured width, wider for a long row, never off-screen. */
function lookupPopperStyle(anchorWidth: number | undefined) {
  return {
    width: 'max-content',
    minWidth: anchorWidth === undefined ? undefined : `${anchorWidth}px`,
    maxWidth: 'min(34rem, calc(100vw - 2rem))',
  } as const
}

/** One suggestion row, keyed by `option.key`, not MUI's label-derived key: two rows can share text (credential
 *  names are not unique), and duplicate sibling keys can drop or duplicate a row. */
function renderLookupOption(
  props: HTMLAttributes<HTMLLIElement> & { key: Key },
  option: LookupOption,
  inputValue: string,
  stacked: boolean,
): JSX.Element {
  const { key: _muiKey, ...rest } = props
  const full = optionText(option)
  return (
    /* `title` shows the whole string; `aria-label` because the highlight splits the label into
       children that accessible-name computation joins with spaces ("Al pha"). */
    <Box component="li" key={option.key} {...rest} title={full} aria-label={full}>
      <Box sx={stacked ? STACKED_ROW_SX : OPTION_ROW_SX}>
        {option.icon && (
          <Box component="span" aria-hidden="true" sx={OPTION_ICON_SX}>
            {option.icon}
          </Box>
        )}
        <Box component="span" sx={OPTION_LABEL_SX}>
          {highlight(option.label, inputValue)}
        </Box>
        {option.hint && (
          <Box component="span" sx={stacked ? STACKED_HINT_SX : OPTION_HINT_SX}>
            {stacked ? highlight(option.hint, inputValue) : option.hint}
          </Box>
        )}
      </Box>
    </Box>
  )
}

/** The state and the `<Autocomplete>` props both lookups share: the option list, the field's measured width
 *  for the popper, the rows, and the Enter guard. `query`/`typed` are the caller's reading of its box (the
 *  single-valued one tells an echo from typing; the many-valued one has only typing). */
export function useLookupField({
  id, testId, fetchOptions, query, typed, minChars, fetchOnFocus, noOptionsText, disabled, fullWidth, size, stacked,
}: Pick<AsyncLookupProps, 'id' | 'testId' | 'fetchOptions'> & {
  query: string
  typed: boolean
  minChars: number
  fetchOnFocus: boolean
  noOptionsText: string
  disabled: boolean
  fullWidth: boolean
  size: 'small' | 'medium'
  stacked: boolean
}) {
  /* The field's own width, so the popup can be at least as wide (see the popper block for why it is
     measured). */
  const rootRef = useRef<HTMLDivElement | null>(null)
  const [anchorWidth, setAnchorWidth] = useState<number | undefined>(undefined)

  const { shown, busy, emptyText, failed, scheduleQuery, requestFirstPage } = useAsyncOptions<LookupOption>({
    fetchOptions, query, typed, minChars, fetchOnFocus, noOptionsText,
  })

  const autocompleteProps = {
    id,
    'data-testid': testId,
    options: shown as LookupOption[],
    disabled,
    fullWidth,
    size,
    loading: busy,
    loadingText: 'Searching…',
    noOptionsText: (
      <Box component="span" sx={failed ? { color: 'error.main' } : undefined}>
        {emptyText}
      </Box>
    ),
    /* The first match is pre-highlighted: type three letters, press Enter. */
    autoHighlight: true,
    openOnFocus: true,
    /* The popup may be WIDER than the field: at a narrow field's width the 40%-capped hint made
       rows like `jaah-github-…` / `GitHub OAuth App…` indistinguishable.
       On the POPPER, not the Paper: a wider paper overflows a field-width popper that popper.js never
       shifts (measured: ran off the window). `style`, not `sx`: MUI writes the anchor width inline.
       `anchorWidth` is measured: `100%` would resolve against the body. */
    slotProps: { popper: { style: lookupPopperStyle(anchorWidth) } },
    /* Measured on open (only read while the popup is up; a resize re-anchors anyway). */
    onOpen: () => setAnchorWidth(rootRef.current?.offsetWidth),
    ref: rootRef,
    isOptionEqualToValue: (a: LookupOption, b: LookupOption) => a.key === b.key,
    getOptionLabel: (option: LookupOption) => option.label,
    getOptionDisabled: (option: LookupOption) => Boolean(option.disabled),
    /* No client filter (the many-valued twin's `filterSelectedOptions` runs first, removing the picked ones):
       the server may match fields this row does not render, and re-filtering on the label would drop those
       hits. */
    filterOptions: (received: LookupOption[]) => received,
    /* Implicit submission: MUI preventDefaults an Enter that SELECTS, but one with nothing highlighted
       would submit the surrounding `<form>` (`TaskDependencies` records the trap). Enter is swallowed
       unconditionally; MUI's selection still runs (it reads `defaultMuiPrevented`, never set here). */
    onKeyDown: (event: KeyboardEvent<HTMLDivElement>) => {
      if (event.key === 'Enter') event.preventDefault()
    },
    renderOption: (props: HTMLAttributes<HTMLLIElement> & { key: Key }, option: LookupOption, state: AutocompleteRenderOptionState) =>
      renderLookupOption(props, option, state.inputValue, stacked),
  }
  return { busy, scheduleQuery, requestFirstPage, autocompleteProps }
}

/** Both lookups' `renderInput`: the field copy, the spinner and the autofill-proof input. What differs —
 *  `required`, when the label is pinned up (`shrink`), a held value's hover `title` — the caller decides. */
export function renderLookupInput(
  params: AutocompleteRenderInputParams,
  { busy, shrink, ariaLabel, title, ...field }: Pick<AsyncLookupProps, 'margin' | 'label' | 'placeholder' | 'required' | 'error' | 'helperText' | 'ariaLabel'> & {
    busy: boolean
    shrink: boolean
    title?: string
  },
): JSX.Element {
  return (
    <TextField
      {...params}
      {...field}
      sx={fieldTapTargetSx}
      slotProps={{
        ...params.slotProps,
        inputLabel: shrink ? { shrink: true } : undefined,
        input: {
          ...params.slotProps.input,
          /* The spinner rides BEFORE MUI's ✕ and ▾, so they never jump sideways. */
          endAdornment: (
            <>
              {busy && <CircularProgress size={16} thickness={5} data-testid="async-lookup-busy" />}
              {params.slotProps.input.endAdornment}
            </>
          ),
        },
        htmlInput: { ...params.slotProps.htmlInput, ...NO_AUTOFILL_INPUT, 'aria-label': ariaLabel, title },
      }}
    />
  )
}

export function AsyncLookup({
  id,
  value,
  onChange,
  fetchOptions,
  label,
  placeholder,
  ariaLabel,
  disabled = false,
  minChars = 1,
  noOptionsText = 'No matches',
  fetchOnFocus = false,
  testId,
  required,
  error,
  helperText,
  fullWidth = true,
  size = 'small',
  margin,
  stacked = false,
  disableClearable = false,
}: AsyncLookupProps): JSX.Element {
  const [inputValue, setInputValue] = useState('')
  /* Whether the box holds a query or an ECHO: MUI writes the picked label back (reason `reset`), which must
     not fire a request or show "loading". A non-typed value counts as the EMPTY query (so `fetchOnFocus`
     re-offers the first page). */
  const [typed, setTyped] = useState(false)

  const { busy, scheduleQuery, requestFirstPage, autocompleteProps } = useLookupField({
    id, testId, fetchOptions, query: typed ? inputValue.trim() : '', typed, minChars, fetchOnFocus, noOptionsText,
    disabled, fullWidth, size, stacked,
  })

  /* The box never shows a value the caller no longer holds. MUI writes the picked label itself and its reset
     effect bails when `value` did not change between renders (`focused && !valueChange`), so a set-and-revert
     in ONE batch (`TaskEditForm`'s From Jira flow on a fast failure) left the label over `null`. Mirrored
     here only while nothing is TYPED. */
  useEffect(() => {
    if (!typed) setInputValue(value === null ? '' : value.label)
  }, [typed, value])

  return (
    <Autocomplete<LookupOption, false, boolean, false>
      {...autocompleteProps}
      /* MUI's clear ✕ is kept (unlike `TaskTags`): it clears one retypable value, and `null` is a legitimate
         state. MUI types the button, so it cannot submit a form. */
      disableClearable={disableClearable}
      value={value}
      inputValue={inputValue}
      onChange={(_event, next) => onChange(next)}
      onInputChange={(_event, next, reason) => {
        if (reason !== 'input') {
          /* A NON-INPUT change is MUI echoing (the picked label, or `''` on clear): mirror the CALLER'S value.
             A consumer whose `value` stays `null` across a pick (`TaskDependencies`' prerequisite lookup) would
             otherwise keep the label and append the next keystroke to it. The `[typed, value]` effect cannot
             catch a click-pick (neither dep changes); same invariant, other path. */
          setInputValue(value === null ? '' : next)
          setTyped(false)
          return
        }
        setInputValue(next)
        setTyped(true)
        // The hook reads its callback from a ref, so the eventual fetch sees the LATEST query.
        scheduleQuery()
      }}
      /* The empty-query fetch rides focus, not the debounce (a single event). */
      onFocus={requestFirstPage}
      renderInput={params => renderLookupInput(params, {
        busy, margin, label, required, error, helperText, placeholder, ariaLabel,
        /* An invisible placeholder is none: MUI hides it while the floating label rests on the input, so
           a real empty answer ("Not connected") showed a blank box until focused (`ui/CredentialSelect`'s
           point 3). `shrink` is pinned only when a placeholder is given — or a value is held: the box's text
           arrives a render late (the mirroring effect above), so on first paint MUI saw it empty and drew
           the label over the picked value (the credential editor's Type). */
        shrink: Boolean(placeholder) || value !== null,
        /* The held value in full on hover: a narrow field ellipsises a long label (a bare email), and the
           closed field never shows the hint. */
        title: value === null ? undefined : optionText(value),
      })}
    />
  )
}

export default AsyncLookup
