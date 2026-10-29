import { cloneElement, type JSX, type ReactElement, type ReactNode } from 'react'
import HelpLabel from './HelpLabel'

/* The console's one form row. THE VALIDATION CONTRACT: `error` is the single source of a field's invalid
 * state, forwarded to the child as MUI `error` + `helperText`. A child must never render its own error
 * text (AC4(e)). `hint` uses the same slot and is suppressed while `error` is set. */
export function FormField({
  label,
  help,
  hint,
  error,
  required,
  children,
}: {
  label: string
  /** A few words shown on hovering the label (`ui/HelpLabel`) — the concise alternative to `hint`. */
  help?: string
  /** Help text under the control. Suppressed while `error` is set. */
  hint?: ReactNode
  /** The invalid-state message. The only source — see the contract above. */
  error?: string
  required?: boolean
  /** Exactly one MUI input (`TextField`, `Select`, `Autocomplete`, …). */
  children: ReactElement
}): JSX.Element {
  // `cloneElement`, not a render prop: a render prop would let a caller skip applying the props.
  const child = children as ReactElement<{ slotProps?: { htmlInput?: Record<string, unknown> } }>
  const slotProps = child.props.slotProps
  return cloneElement(child as ReactElement<Record<string, unknown>>, {
    label: help ? <HelpLabel label={label} help={help} /> : label,
    /* The help text is also the control's accessible description (TextField `htmlInput` slot only); set
       only when there is help so the child's own slot props are not overwritten. */
    ...(help ? { slotProps: { ...slotProps, htmlInput: { ...slotProps?.htmlInput, 'aria-description': help } } } : {}),
    required,
    error: Boolean(error),
    helperText: error || hint,
    fullWidth: true,
    size: 'small',
  })
}

export default FormField
