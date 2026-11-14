import { useId, type JSX, type ReactNode } from 'react'
import Box from '@mui/material/Box'
import InputAdornment from '@mui/material/InputAdornment'
import TextField, { type TextFieldProps } from '@mui/material/TextField'
import Tip from './Tip'
import FormField from './FormField'
import type { ChoiceHelp, HelpProp } from './explain'
import { fieldTapTargetSx, SR_ONLY_SX } from './listStyles'
import { LINE_HEIGHT, grid } from '../theme'

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
 *   `options`, so a placeholder or a stale stored value can lead the list.
 * - `warning` marks a value that saves but will not work: a yellow outline and a ⚠ whose hover says why. It
 *   never blocks; an `error` outranks it. The ⚠ is focusable and carries the text as its name, and the input is described by it. Shown on a read-only field too: the
 *   viewer still learns the setting is broken. A glyph, not an
 *   icon component (`@mui/icons-material` is not a dependency). */

/** What an empty read-only date/time field shows. */
export const NOT_SET = 'Not set'

const DATE_TYPES = ['date', 'time', 'datetime-local', 'month']

/** `help`: what picking the option does — a line of the label's pop-up (`ui/HelpLabel`), never a hover on the
 *  native `<option>`. Give `label` as a string when there is `help`; the line shows it. */
export type FormOption = { value: string; label: ReactNode; disabled?: boolean; help?: string }

export type FormTextFieldProps = Omit<
  TextFieldProps,
  'value' | 'onChange' | 'label' | 'error' | 'helperText' | 'required' | 'children'
> & {
  label: string
  value: string | number
  onChange: (value: string) => void
  /** The label's explanation (`ui/HelpLabel`): a tooltip, and a pop-up when it says more. */
  help?: HelpProp
  hint?: ReactNode
  /** A validation message for this field. Replaces `hint` while set, and reds the control. */
  error?: string
  /** A value that saves but will not work: yellow, with this text on hovering the ⚠ (see above). */
  warning?: string
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
  warning,
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
  const warn = warning && !error ? warning : undefined
  const warnId = `${useId()}-warning`
  const choices: ChoiceHelp[] = (options ?? []).flatMap(o => (o.help && typeof o.label === 'string' ? [{ label: o.label, help: o.help }] : []))
  const htmlInput = {
    ...slots?.htmlInput,
    ...(warn ? { 'aria-describedby': warnId } : {}),
    ...(testId ? { 'data-testid': testId } : {}),
    ...(need && !requiredMark ? { required: true } : {}),
  }
  return (
    <FormField label={label} help={help} choices={choices} hint={hint} error={error} required={need && requiredMark}>
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
          ...(warn ? { input: { ...slots?.input, endAdornment: <WarningMark text={warn} descriptionId={warnId} /> } } : {}),
          htmlInput,
        }}
        sx={[fieldTapTargetSx, ...(warn ? [WARNING_SX] : []), ...(Array.isArray(sx) ? sx : sx ? [sx] : [])]}
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

/* The outline and label in the theme's warning colour, focused or not. */
const WARNING_SX = {
  '& .MuiOutlinedInput-notchedOutline, & .MuiOutlinedInput-root:hover .MuiOutlinedInput-notchedOutline, & .MuiOutlinedInput-root.Mui-focused .MuiOutlinedInput-notchedOutline': {
    borderColor: 'warning.main',
  },
  '& .MuiInputLabel-root, & .MuiInputLabel-root.Mui-focused': { color: 'warning.main' },
} as const

function WarningMark({ text, descriptionId }: { text: string; descriptionId: string }): JSX.Element {
  return (
    <InputAdornment position="end">
      {/* The input's description too (`aria-describedby`): focusing the field reads the warning. */}
      <Box component="span" id={descriptionId} sx={SR_ONLY_SX}>{text}</Box>
      <Tip describeChild={false} title={text}>
        {/* Focusable and named, so a keyboard or screen-reader user reaches the same text the hover shows. */}
        <Box
          component="span"
          role="img"
          aria-label={text}
          tabIndex={0}
          data-testid="field-warning"
          sx={{ color: 'warning.main', cursor: 'help', fontSize: grid(3), lineHeight: LINE_HEIGHT.tight }}
        >
          {'\u26A0\uFE0E' /* text presentation: an emoji ⚠ ignores the colour */}
        </Box>
      </Tip>
    </InputAdornment>
  )
}

export default FormTextField
