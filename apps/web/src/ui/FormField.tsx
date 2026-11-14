import { cloneElement, type JSX, type ReactElement, type ReactNode } from 'react'
import HelpLabel, { ExplainMoreButton } from './HelpLabel'
import { toExplain, type ChoiceHelp, type HelpProp } from './explain'

/** The child's slot props `FormField` reads and extends. */
type SlotProps = { htmlInput?: Record<string, unknown>; select?: { native?: boolean; SelectDisplayProps?: Record<string, unknown> } }

/* The console's one form row. THE VALIDATION CONTRACT: `error` is the single source of a field's invalid
 * state, forwarded to the child as MUI `error` + `helperText`. A child must never render its own error
 * text (AC4(e)). `hint` uses the same slot and is suppressed while `error` is set. */
export function FormField({
  label,
  help,
  choices,
  hint,
  error,
  required,
  children,
}: {
  label: string
  /** The label's explanation (`ui/HelpLabel`): a tooltip, and a pop-up when it says more. */
  help?: HelpProp
  /** One pop-up line per choice (a select's options with `help`). */
  choices?: readonly ChoiceHelp[]
  /** Help text under the control. Suppressed while `error` is set. */
  hint?: ReactNode
  /** The invalid-state message. The only source — see the contract above. */
  error?: string
  required?: boolean
  /** Exactly one MUI input (`TextField`, `Select`, `Autocomplete`, …). */
  children: ReactElement
}): JSX.Element {
  // `cloneElement`, not a render prop: a render prop would let a caller skip applying the props.
  const child = children as ReactElement<{ select?: boolean; slotProps?: SlotProps }>
  const slotProps = child.props.slotProps
  const tip = toExplain(help)?.tip
  /* The tip is also the control's accessible description, on its FOCUSABLE element: a TextField's input, or —
     for a non-native `select` — the display `div` (`SelectDisplayProps`), since MUI's hidden `<input>` is never
     read. An `AsyncLookup` forwards `htmlInput` to its own input. Set only when there is help, so the child's
     own slot props are not overwritten. */
  const describe = { 'aria-description': tip }
  const nonNativeSelect = Boolean(child.props.select) && !slotProps?.select?.native
  const described: SlotProps | undefined = !tip
    ? undefined
    : nonNativeSelect
      ? { ...slotProps, select: { ...slotProps?.select, SelectDisplayProps: { ...slotProps?.select?.SelectDisplayProps, ...describe } } }
      : { ...slotProps, htmlInput: { ...slotProps?.htmlInput, ...describe } }
  const field = cloneElement(child as ReactElement<Record<string, unknown>>, {
    label: tip ? <HelpLabel label={label} help={help} choices={choices} /> : label,
    ...(described ? { slotProps: described } : {}),
    required,
    error: Boolean(error),
    helperText: error || hint,
    fullWidth: true,
    size: 'small',
  })
  // The keyboard way to the pop-up, after the field and outside its `<label>`; nothing when there is no more.
  return <>{field}<ExplainMoreButton label={label} help={help} choices={choices} /></>
}

export default FormField
