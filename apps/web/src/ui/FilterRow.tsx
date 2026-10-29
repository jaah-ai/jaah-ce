import type { JSX, ReactNode } from 'react'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import { fieldTapTargetSx, tapTargetSx } from './listStyles'
import { NO_AUTOFILL_INPUT } from './noAutofill'

/* ONE wrapping row of controls, handed to `ui/FilterBar` as a SINGLE child: the bar stacks each child
   full-width below `sm`, which put six controls on six lines at 390px (measured 695px of chrome vs a 529px
   baseline, a 31% AC5 regression). `ui/FilterBar` is frozen, so the grouping happens here: every control
   stays visible, packed two per line on phones. A wrapped line's outlined labels ride ~9px above their fields, so
   the row gap is 16px, not the 8px column gap: at 8px a label sat on the field above it. */
export function FilterRow({ children }: { children: ReactNode }): JSX.Element {
  return (
    <Stack
      direction="row"
      spacing={1}
      useFlexGap
      sx={{ flexWrap: 'wrap', alignItems: 'center', width: '100%', minWidth: 0, rowGap: 2 }}
    >
      {children}
    </Stack>
  )
}

type FilterOption = { value: string; label: string }

/** A filter value matching no option is appended AS ITSELF: a native `<select>` with an unmatched value
 *  renders BLANK while the state goes on filtering by it (a stale bookmark, a hand-edited URL, a deleted
 *  row, a value before its roster lands), so an applied filter never vanishes from its own control. */
export function withUnmatched(options: FilterOption[], value: string): FilterOption[] {
  if (!value || options.some(option => option.value === value)) return options
  return [...options, { value, label: value }]
}

/** A server vocabulary as options, the active value unioned in (`withUnmatched`'s rule); `sort` orders the
 *  union, `label` words each token. */
export function vocabularyOptions(
  values: readonly string[],
  active: string,
  { label = (value: string) => value, sort = false }: { label?: (value: string) => string; sort?: boolean } = {},
): FilterOption[] {
  const names = Array.from(new Set(values))
  if (active && !names.includes(active)) names.push(active)
  if (sort) names.sort()
  return names.map(value => ({ value, label: label(value) }))
}

/* The console's ONE filter dropdown: MUI `TextField select`, `native` (a real `<select>`). Native because
   the vocabulary has no icons, no multi-select and no async load — the Material answer for that, the
   phone's own picker, and what the suite drives with `userEvent.selectOptions`.
   `ariaLabel` is a SEPARATE, frozen accessible name specs query by, applied to the `select` itself.
   `disabled`/`errorText` do not widen the vocabulary: a caller with fetched options must have them before
   mapping (the Credentials strip fetches once at mount); they render the never-arrived roster honestly.
   Additive and default-absent, so existing call sites are unchanged (N13: not forked). */
export function FilterSelect({
  label,
  ariaLabel,
  value,
  onChange,
  allLabel,
  options,
  minWidth = 150,
  maxWidth,
  grow = false,
  disabled = false,
  errorText,
}: {
  label: string
  /** The frozen accessible name (e.g. "Filter by repo"). Omit to let the visible label name it. */
  ariaLabel?: string
  value: string
  onChange: (next: string) => void
  /** The "no filter" option's copy (e.g. "All repos"). */
  allLabel: string
  options: { value: string; label: string }[]
  minWidth?: number
  /** A cap for a roster with long labels: a native select is as wide as its longest option, which would
   *  otherwise push the control onto a row of its own. The closed value truncates; the list stays whole. */
  maxWidth?: number
  /** Size from `minWidth` and share the line's slack (up to `maxWidth`) instead of from the longest option, so
   *  a wrapped line fills its width rather than stranding one control at its left. */
  grow?: boolean
  /** Unusable while a roster loads or after it failed. Stays MOUNTED with its label, so the axis stays
   *  visible. */
  disabled?: boolean
  /** WHY it is unusable, in words (R2.9: "visible sentences, not a silently empty list"); absent on the
   *  happy path so no caller pays ~20px of helper height. */
  errorText?: string
}): JSX.Element {
  return (
    <TextField
      select
      label={label}
      value={value}
      onChange={e => onChange(e.target.value)}
      disabled={disabled}
      error={Boolean(errorText)}
      helperText={errorText}
      slotProps={{
        select: { native: true },
        // Pinned open: an outlined select with value '' would otherwise overlap label and placeholder.
        inputLabel: { shrink: true },
        htmlInput: ariaLabel ? { 'aria-label': ariaLabel } : {},
      }}
      /* `m: 0` cancels the theme's `margin: 'dense'` (chrome height above the first row, AC5). */
      /* The floor goes on the `input` slot too: for a `native` select the tap target is the `<select>`
         (40px inside a 44px root). Spelled as PADDING, not `minHeight`: forcing `border-box` for a
         `minHeight` collapsed the 23px content to 6px and rendered the value as three dots (caught only
         by looking). 23px content + 21px padding = 44px. */
      sx={theme => ({
        minWidth,
        maxWidth,
        ...(grow && { flex: `1 1 ${minWidth}px` }),
        m: 0,
        '& .MuiInputBase-root': tapTargetSx(theme),
        '& .MuiInputBase-input': { pt: { xs: '10.5px', sm: '8.5px' }, pb: { xs: '10.5px', sm: '8.5px' } },
      })}
    >
      <option value="">{allLabel}</option>
      {options.map(o => (
        <option key={o.value} value={o.value}>{o.label}</option>
      ))}
    </TextField>
  )
}

/* THE ONE CLIENT-SIDE LIST SEARCH: an undebounced "Search" field that grows from its `minWidth` basis
   (190px by default). A server search is `ui/SearchBox` (debounced) — keep the two apart. */
export function FilterSearch({ value, onChange, ariaLabel, placeholder, minWidth = 190 }: {
  value: string
  onChange: (next: string) => void
  ariaLabel: string
  placeholder: string
  /** The flex basis and floor; a strip packing many controls onto one line lowers it. */
  minWidth?: number
}): JSX.Element {
  return (
    <TextField
      label="Search"
      value={value}
      onChange={e => onChange(e.target.value)}
      placeholder={placeholder}
      slotProps={{
        inputLabel: { shrink: true },
        htmlInput: { ...NO_AUTOFILL_INPUT, 'aria-label': ariaLabel },
      }}
      sx={[{ flex: `1 1 ${minWidth}px`, minWidth, m: 0 }, fieldTapTargetSx]}
    />
  )
}

/** Does any field contain the trimmed query, case-insensitively? A blank query matches everything. */
export function matchesAny(q: string, ...fields: (string | null | undefined)[]): boolean {
  const needle = q.trim().toLowerCase()
  return !needle || fields.some(field => (field ?? '').toLowerCase().includes(needle))
}
