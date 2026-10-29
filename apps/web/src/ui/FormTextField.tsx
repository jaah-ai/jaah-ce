import type { JSX, ReactNode } from 'react'
import TextField, { type TextFieldProps } from '@mui/material/TextField'
import FormField from './FormField'
import { fieldTapTargetSx } from './listStyles'

/* The console's ONE label+field ROW: `ui/FormField` plus its `TextField`, so a form never re-spells the
 * wrapper, the tap-target floor or the `e.target.value` unwrap. Every other `TextField` prop passes through.
 *
 * - `hint`/`error`/`help`/`required` belong to `FormField`, the single owner of that slot (the error
 *   REPLACES the hint). `required` drops while `readOnly`: a read-only field can arrive EMPTY, and an
 *   unfillable `required` would wedge the whole form. `requiredMark={false}` keeps the native `required`
 *   but draws no asterisk (`ProjectEditPage`, whose labels are matched exactly).
 * - `readOnly` renders as MUI `disabled`, not `htmlInput.readOnly`: a read-only input looks editable, and
 *   `disabled` is the console's existing answer.
 * - An EMPTY read-only date/time field reads `Not set` (`NOT_SET`), as text: the browser's own mask
 *   ("mm/dd/yyyy") would show through `disabled` as if it were a value.
 * - `testId` lands on the native `<input>`/`<select>` (`htmlInput`), where tests and e2e probes read it.
 * - `options` (or `select` with `<option>` children) makes it a NATIVE select, like every select in this
 *   app: phones get their own picker, and the `id` lands on the `<select>`. `children` render before
 *   `options`, so a placeholder or a stale stored value can lead the list. */

/** What an empty read-only date/time field shows. */
export const NOT_SET = 'Not set'

const DATE_TYPES = ['date', 'time', 'datetime-local', 'month']

export type FormOption = { value: string; label: ReactNode; disabled?: boolean }

export type FormTextFieldProps = Omit<
  TextFieldProps,
  'value' | 'onChange' | 'label' | 'error' | 'helperText' | 'required' | 'children'
> & {
  label: string
  value: string | number
  onChange: (value: string) => void
  /** A few words shown on hovering the label (`ui/HelpLabel`). */
  help?: string
  hint?: ReactNode
  /** A validation message for this field. Replaces `hint` while set, and reds the control. */
  error?: string
  required?: boolean
  /** Draw the label's asterisk for `required` (default). */
  requiredMark?: boolean
  /** Show the value, refuse the edit (renders `disabled`; see above). */
  readOnly?: boolean
  testId?: string
  options?: readonly FormOption[]
  children?: ReactNode
}

export function FormTextField({
  label,
  value,
  onChange,
  help,
  hint,
  error,
  required,
  requiredMark = true,
  readOnly,
  testId,
  options,
  children,
  select,
  disabled,
  slotProps,
  sx,
  ...rest
}: FormTextFieldProps): JSX.Element {
  const native = select || options !== undefined
  const slots = slotProps as Record<string, Record<string, unknown> | undefined> | undefined
  const need = Boolean(required) && !readOnly
  /* The label must sit shrunk wherever the control shows text while empty — an option's text, a
     placeholder, a browser date mask: unshrunk, MUI draws it over that text (or hides the placeholder). */
  const dateLike = DATE_TYPES.includes(String(rest.type))
  const shrink = native || Boolean(rest.placeholder) || dateLike
  const notSet = Boolean(readOnly) && dateLike && value === ''
  const htmlInput = {
    ...slots?.htmlInput,
    ...(testId ? { 'data-testid': testId } : {}),
    ...(need && !requiredMark ? { required: true } : {}),
  }
  return (
    <FormField label={label} help={help} hint={hint} error={error} required={need && requiredMark}>
      <TextField
        {...rest}
        type={notSet ? 'text' : rest.type}
        value={notSet ? NOT_SET : value}
        disabled={disabled || readOnly}
        onChange={e => onChange(e.target.value)}
        select={native}
        slotProps={{
          ...slots,
          ...(native ? { select: { ...slots?.select, native: true } } : {}),
          ...(shrink ? { inputLabel: { shrink: true, ...slots?.inputLabel } } : {}),
          htmlInput,
        }}
        sx={[fieldTapTargetSx, ...(Array.isArray(sx) ? sx : sx ? [sx] : [])]}
      >
        {children}
        {options?.map(o => (
          <option key={o.value} value={o.value} disabled={o.disabled}>
            {o.label}
          </option>
        ))}
      </TextField>
    </FormField>
  )
}

export default FormTextField
