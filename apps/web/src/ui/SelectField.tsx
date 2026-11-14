import type { JSX, ReactNode } from 'react'
import TextField, { type TextFieldProps } from '@mui/material/TextField'
import type { Theme } from '@mui/material/styles'
import { fieldTapTargetSx } from './listStyles'
import Tip from './Tip'

export type SelectOption = { value: string; label: string; disabled?: boolean }

/* THE CONSOLE'S ONE DROPDOWN (style guide § Controls): an outlined MUI `TextField select`, always `native` — a real
 * `<select>`, so a phone opens its own picker and the suite drives it with `userEvent.selectOptions`. Options are
 * plain text (`options`, or `<option>`/`<optgroup>` children): no icons, no two-line rows, no `renderValue`.
 * `ui/FilterRow`'s `FilterSelect` (a filter strip) and `ui/FormTextField` (a labelled form row) are its two
 * composites; reach for this one anywhere else.
 * `dense` is the in-line size (a chart header, a pager, a field cell): no vertical padding above the phone; the
 * 44px tap floor holds on the phone either way. `ariaLabel` names a label-less select; `testId` lands on the
 * `<select>` itself. Every other `TextField` prop passes through. */
export interface SelectFieldProps extends Omit<TextFieldProps, 'select' | 'value' | 'onChange' | 'children'> {
  value: string
  onChange: (value: string) => void
  options?: readonly SelectOption[]
  children?: ReactNode
  ariaLabel?: string
  testId?: string
  dense?: boolean
}

/* Keyed on `sm` up, the exact complement of `fieldTapTargetSx`'s `down('sm')`, so the floor is never undone. */
const denseSx = (theme: Theme) => ({
  [theme.breakpoints.up('sm')]: { '& .MuiNativeSelect-select': { py: 0, minHeight: 0 } },
})

export default function SelectField({
  value,
  onChange,
  options,
  children,
  ariaLabel,
  testId,
  dense = false,
  label,
  slotProps,
  sx,
  size = 'small',
  title,
  ...rest
}: SelectFieldProps): JSX.Element {
  const slots = slotProps as Record<string, Record<string, unknown> | undefined> | undefined
  return (
    <Tip title={title}><TextField
      {...rest}
      select
      size={size}
      label={label}
      value={value}
      onChange={e => onChange(e.target.value)}
      slotProps={{
        ...slots,
        select: { ...slots?.select, native: true },
        // Pinned open: an outlined native select always shows an option's text, which an unshrunk label overlaps.
        ...(label ? { inputLabel: { shrink: true, ...slots?.inputLabel } } : {}),
        htmlInput: {
          ...slots?.htmlInput,
          ...(ariaLabel ? { 'aria-label': ariaLabel } : {}),
          ...(testId ? { 'data-testid': testId } : {}),
        },
      }}
      sx={[{ m: 0 }, fieldTapTargetSx, dense && denseSx, ...(Array.isArray(sx) ? sx : sx ? [sx] : [])]}
    >
      {children}
      {options?.map(o => (
        <option key={o.value} value={o.value} disabled={o.disabled}>{o.label}</option>
      ))}
    </TextField></Tip>
  )
}
