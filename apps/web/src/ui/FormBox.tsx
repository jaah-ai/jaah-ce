import type { FormEvent, JSX } from 'react'
import Box, { type BoxProps } from '@mui/material/Box'

/* The console's ONE inline `<form>`: a `Box` rendered as a form, `noValidate` (the console validates its own
 * fields), whose submit swallows the browser's navigation and runs `onSubmit`. Every other `Box` prop passes
 * through, so a caller styles it like any box. Before this, each inline form re-spelled the five-line
 * `component="form" noValidate onSubmit={e => { e.preventDefault(); void save() }}` block. */

export type FormBoxProps = Omit<BoxProps<'form'>, 'component' | 'onSubmit'> & {
  /** Runs on submit (Enter in a field, or the submit button). A returned promise is awaited by nobody. */
  onSubmit: () => void | Promise<unknown>
}

export function FormBox({ onSubmit, children, ...rest }: FormBoxProps): JSX.Element {
  const submit = (e: FormEvent<HTMLFormElement>): void => {
    e.preventDefault()
    void onSubmit()
  }
  return (
    <Box component="form" noValidate onSubmit={submit} {...rest}>
      {children}
    </Box>
  )
}

export default FormBox
