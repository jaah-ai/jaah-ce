import { useState } from 'react'
import FormTextField from '../ui/FormTextField'
import { ShowHideAdornment } from '../ui/ShowHideAdornment'
import { tapFloorInput } from './authEntry'

/* The server's rule (`apps/api/app/auth.py::password_problem`), checked here only to answer sooner; the
 * server's sentence wins whenever it refuses. */
export const PASSWORD_MIN = 8
export const TOO_SHORT_COPY = `Use at least ${PASSWORD_MIN} characters.`
export const MISMATCH_COPY = "Passwords don't match."

export type PasswordErrors = { password?: string; confirm?: string }

/** A new password's field errors; `{}` when it passes. `confirm` defaults to the password for a form without one. */
export function passwordProblem(password: string, confirm = password): PasswordErrors {
  const errors: PasswordErrors = {}
  // Characters as the server counts them (code points), so an emoji is one, not two.
  if ([...password].length < PASSWORD_MIN) errors.password = TOO_SHORT_COPY
  if (confirm !== password) errors.confirm = MISMATCH_COPY
  return errors
}

/** The form's state: the password, its confirmation and their field errors. */
export function useNewPassword() {
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [errors, setErrors] = useState<PasswordErrors>({})
  return { password, setPassword, confirm, setConfirm, errors, setErrors }
}

type Props = ReturnType<typeof useNewPassword> & {
  /** Prefix of the two inputs' ids (`<idPrefix>-password`, `<idPrefix>-confirm`). */
  idPrefix: string
  label: string
}

/** A new password typed twice, with one Show/Hide for both; typing in a field clears that field's error. */
export default function NewPasswordFields({ idPrefix, label, password, setPassword, confirm, setConfirm, errors, setErrors }: Props) {
  const [shown, setShown] = useState(false)
  const type = shown ? 'text' : 'password'
  const toggle = <ShowHideAdornment shown={shown} onToggle={() => setShown(s => !s)} what="password" />
  return (
    <>
      <FormTextField
        label={label} required error={errors.password} hint={TOO_SHORT_COPY}
        id={`${idPrefix}-password`}
        type={type}
        value={password}
        onChange={v => {
          setPassword(v)
          setErrors(prev => ({ ...prev, password: undefined }))
        }}
        autoFocus
        autoComplete="new-password"
        margin="none"
        slotProps={{ input: { endAdornment: toggle } }}
        sx={tapFloorInput}
      />
      <FormTextField
        label="Confirm password" required error={errors.confirm}
        id={`${idPrefix}-confirm`}
        type={type}
        value={confirm}
        onChange={v => {
          setConfirm(v)
          setErrors(prev => ({ ...prev, confirm: undefined }))
        }}
        autoComplete="new-password"
        margin="none"
        sx={tapFloorInput}
      />
    </>
  )
}
