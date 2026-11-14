import type { JSX, ReactNode } from 'react'
import Stack from '@mui/material/Stack'
import { fieldTapTargetSx } from './listStyles'
import SearchBox from './SearchBox'
import SelectField from './SelectField'

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

/* The console's ONE filter dropdown: `ui/SelectField` (a native `<select>`) with the "no filter" option first.
   `ariaLabel` is a SEPARATE, frozen accessible name specs query by, applied to the `select` itself.
   `disabled`/`errorText` do not widen the vocabulary: a caller with fetched options must have them before
   mapping (the Credentials strip fetches once at mount); they render the never-arrived roster honestly.
   The phone tap floor is `SelectField`'s (`fieldTapTargetSx`): a single native select has no MUI `height`
   rule to collide with, so the 23px value keeps 27px of content inside the 44px floor and never ellipsises. */
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
    <SelectField
      label={label}
      ariaLabel={ariaLabel}
      value={value}
      onChange={onChange}
      disabled={disabled}
      error={Boolean(errorText)}
      helperText={errorText}
      options={[{ value: '', label: allLabel }, ...options]}
      sx={{ minWidth, maxWidth, ...(grow && { flexGrow: 1, flexShrink: 1, flexBasis: minWidth }) }}
    />
  )
}

/* THE CLIENT-SIDE LIST SEARCH: `ui/SearchBox` (the console's one search box) under a "Search" label, committing
   every keystroke (the list filters in the browser), growing from its `minWidth` basis (190px by default). */
export function FilterSearch({ value, onChange, ariaLabel, placeholder, minWidth = 190, grow = 1 }: {
  value: string
  onChange: (next: string) => void
  ariaLabel: string
  placeholder: string
  /** The flex basis and floor; a strip packing many controls onto one line lowers it. */
  minWidth?: number
  /** Its share of the row's spare width against the strip's other growing controls (each 1). */
  grow?: number
}): JSX.Element {
  return (
    <SearchBox
      label="Search"
      value={value}
      onChange={onChange}
      delayMs={0}
      placeholder={placeholder}
      ariaLabel={ariaLabel}
      sx={[{ flexGrow: grow, flexShrink: 1, flexBasis: minWidth, minWidth, m: 0 }, fieldTapTargetSx]}
    />
  )
}

/** Does any field contain the trimmed query, case-insensitively? A blank query matches everything. */
export function matchesAny(q: string, ...fields: (string | null | undefined)[]): boolean {
  const needle = q.trim().toLowerCase()
  return !needle || fields.some(field => (field ?? '').toLowerCase().includes(needle))
}
