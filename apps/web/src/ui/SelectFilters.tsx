import type { JSX } from 'react'
import Box from '@mui/material/Box'
import { FilterSelect, withUnmatched } from './FilterRow'
import ToolbarMenu, { filterRadioPage, PAGE_CAPTION_SX, type ToolbarMenuItem } from './ToolbarMenu'

/** One narrowing: a select on a desktop, a radio page in the phone's menu. */
export type SelectNarrowing<K extends string> = {
  key: K
  label: string
  /** The "no filter" option's copy (e.g. "Any status"). */
  allLabel: string
  options: { value: string; label: string }[]
  /** The phone page's test id; its options are `${testId}-${value}`, the all choice `${testId}-any`. */
  testId: string
  ariaLabel?: string
  minWidth?: number
  maxWidth?: number
  grow?: boolean
  disabled?: boolean
  /** Why the roster never arrived: the select's helper text, and on a phone the page's one sentence. */
  errorText?: string
  /** A rarer narrowing: on a desktop it sits behind one More filters menu, so the row keeps Search wide. */
  more?: boolean
}

/* A LIST PAGE'S SELECT NARROWINGS, drawn once for both widths. On a desktop each is a `FilterSelect` (the rarer,
 * `more` ones behind one More filters menu: five selects took the whole row at 1440px). ON A PHONE every one
 * folds into ONE sliders menu (`ui/ToolbarMenu`) beside Search: selects beside Search took two or three lines
 * above the first row. The menu's badge counts the narrowings on (Search shows itself), and its Clear resets
 * exactly those. One-off controls (a search, a lookup, the page's verb) stay beside it, the caller's. */
export function SelectFilters<K extends string>({ narrowings, values, onChange, onClear, phone, menuName }: {
  narrowings: SelectNarrowing<K>[]
  values: Record<K, string>
  onChange: (key: K, value: string) => void
  onClear: (keys: K[]) => void
  phone: boolean
  /** The phone menu's accessible name (e.g. "Filter nodes"). */
  menuName: string
}): JSX.Element {
  const menu = (name: string, heading: string, shown: SelectNarrowing<K>[]) => (
    <ToolbarMenu
      name={name}
      heading={heading}
      hint={heading}
      badgeCount={shown.filter(n => values[n.key]).length}
      items={shown.map(n => page(n))}
      onClear={() => onClear(shown.map(n => n.key))}
    />
  )
  const page = (n: SelectNarrowing<K>): ToolbarMenuItem => n.errorText
    ? {
        kind: 'page',
        id: n.key,
        label: n.label,
        value: 'Unavailable',
        render: () => <Box data-testid={`${n.testId}-error`} sx={{ ...PAGE_CAPTION_SX, pt: 1.5, color: 'error.main' }}>{n.errorText}</Box>,
      }
    : filterRadioPage(n.testId, n.key, n.label, n.allLabel, n.options, values[n.key], value => onChange(n.key, value))
  if (phone) return menu(menuName, 'Filters', narrowings)
  const more = narrowings.filter(n => n.more)
  return (
    <>
      {narrowings.filter(n => !n.more).map(n => (
        <FilterSelect
          key={n.key}
          label={n.label}
          ariaLabel={n.ariaLabel}
          value={values[n.key]}
          onChange={value => onChange(n.key, value)}
          allLabel={n.allLabel}
          options={withUnmatched(n.options, values[n.key])}
          minWidth={n.minWidth}
          maxWidth={n.maxWidth}
          grow={n.grow}
          disabled={n.disabled}
          errorText={n.errorText}
        />
      ))}
      {more.length > 0 && menu('More filters', 'More filters', more)}
    </>
  )
}
