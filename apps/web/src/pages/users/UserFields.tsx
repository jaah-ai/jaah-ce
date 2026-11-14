import { type FormEvent, type ReactNode, useCallback, useState } from 'react'
import Box from '@mui/material/Box'
import { readUserSettings, type UserSettingsDraft } from '../../lib/userSettings'
import FormTextField from '../../ui/FormTextField'
import { PASSWORD_MIN, passwordProblem } from '../NewPasswordFields'

// What `/users/:id` and `/profile` both edit; each page keeps its own endpoint, chrome and save.

/** The name fields' tip (`identities.person_label`): the name, else the email, labels the person everywhere. */
const NAME_HELP = 'Shown for this account everywhere instead of the email.'

type StoredNames = { first_name?: string | null; last_name?: string | null }

/* Settings is ONE draft: its shared component returns the whole next value. */
export function useUserDrafts() {
  const [firstName, setFirstName] = useState('')
  const [lastName, setLastName] = useState('')
  const [password, setPassword] = useState('')
  const [settings, setSettings] = useState<UserSettingsDraft>(() => readUserSettings(null))
  const seedNames = useCallback((u: StoredNames) => { setFirstName(u.first_name ?? ''); setLastName(u.last_name ?? ''); setPassword('') }, [])
  /* Only changed names; the password only when typed. */
  const nameChanges = (stored: StoredNames): Record<string, unknown> => ({
    ...(firstName !== (stored.first_name ?? '') ? { first_name: firstName } : {}),
    ...(lastName !== (stored.last_name ?? '') ? { last_name: lastName } : {}),
    ...(password ? { password } : {}),
  })
  return { firstName, setFirstName, lastName, setLastName, password, setPassword, seedNames, nameChanges,
    settings, setSettings }
}

/** The name pair's state: `useUserDrafts`'s, or a page's own two states (the create wizard). */
type Names = Pick<ReturnType<typeof useUserDrafts>, 'firstName' | 'setFirstName' | 'lastName' | 'setLastName'>

/* First and last name: one row from 600px (two short fields side by side), stacked on a phone. `gap` matches the
 * form's. `optional` says so in the labels — the create wizard, where the email beside them is required. */
export function NameFields({ idPrefix, disabled, names, optional = false }:
  { idPrefix: string; disabled: boolean; names: Names; optional?: boolean }) {
  const suffix = optional ? ' (optional)' : ''
  return (
    <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr' }, gap: 1.5 }}>
      <FormTextField label={`First name${suffix}`} help={NAME_HELP} id={`${idPrefix}-first-name`} disabled={disabled} type="text" value={names.firstName} onChange={names.setFirstName} />
      <FormTextField label={`Last name${suffix}`} help={NAME_HELP} id={`${idPrefix}-last-name`} disabled={disabled} type="text" value={names.lastName} onChange={names.setLastName} />
    </Box>
  )
}

/* The names, then the new password; `children` sit between them (UserEditPage's Description). */
export function NameAndPasswordFields({ idPrefix, disabled, drafts, children }:
  { idPrefix: string; disabled: boolean; drafts: ReturnType<typeof useUserDrafts>; children?: ReactNode }) {
  return (
    <>
      <NameFields idPrefix={idPrefix} disabled={disabled} names={drafts} />
      {children}
      <FormTextField
        label="Set new password" help="Replaces the sign-in password and forgets every trusted device." id={`${idPrefix}-password`} disabled={disabled} type="password" value={drafts.password}
        onChange={drafts.setPassword} placeholder="Leave blank to keep unchanged" autoComplete="new-password"
        slotProps={{ htmlInput: { minLength: PASSWORD_MIN } }}
      />
    </>
  )
}

/** The password floor's refusal, or `''`: the rule the input declares (`minLength`), enforced by hand — see
 *  `submitUserForm`. */
export function passwordFloorError(password: string): string {
  return password && passwordProblem(password).password ? `The new password must be at least ${PASSWORD_MIN} characters.` : ''
}

/* The one save's glue (one button, one request, on both pages). One form spans three panels and hidden panels
 * stay mounted: native validation cannot focus a control in a `display:none` panel, so it ABORTS the submit ("An
 * invalid form control is not focusable") — no request, no banner, a dead button. So each form is `noValidate`
 * and `validate` (the first broken field, or `''`) enforces the rules the inputs declare (the password floor); the
 * attributes stay for semantics. A refusal is reported on the tab that owns the field (Profile): switch there.
 * Backstop: an empty write must never report "Saved" (implicit submission already does nothing with the button
 * disabled). Then `write` (the page's own request) runs under `run`. */
export async function submitUserForm<T>(e: FormEvent, current: T | null, form: {
  selectTab: (tab: 'profile') => void
  setSaveError: (message: string) => void
  clearSaved: () => void
  run: (fallback: string, write: () => Promise<unknown>) => Promise<unknown>
  validate: () => string
  buildBody: (current: T) => Record<string, unknown>
  write: (body: Record<string, unknown>, current: T) => Promise<void>
}): Promise<void> {
  e.preventDefault()
  if (!current) return
  const invalid = form.validate()
  if (invalid) {
    form.selectTab('profile')
    form.setSaveError(invalid)
    form.clearSaved()
    return
  }
  const body = form.buildBody(current)
  if (Object.keys(body).length === 0) return
  await form.run('Failed to save', () => form.write(body, current))
}

