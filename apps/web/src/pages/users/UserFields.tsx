import { type ReactNode, useCallback, useState } from 'react'
import Box from '@mui/material/Box'
import type { FocusableProject } from '../../components/UserSettingsFields'
import { useApiRead } from '../../hooks/useApiRead'
import { listProjects } from '../../hooks/useRosters'
import { readNotifications } from '../../lib/notificationPrefs'
import { readUserSettings, type UserSettingsDraft } from '../../lib/userSettings'
import type { UserNotificationsPreference } from '../../types'
import FormTextField from '../../ui/FormTextField'
import { PASSWORD_MIN } from '../NewPasswordFields'

// What `/users/:id` and `/profile` both edit; each page keeps its own endpoint, chrome and save.

type StoredNames = { first_name?: string | null; last_name?: string | null }

/* Settings and notifications are ONE draft each: their shared components return the whole next value. */
export function useUserDrafts() {
  const [firstName, setFirstName] = useState('')
  const [lastName, setLastName] = useState('')
  const [password, setPassword] = useState('')
  const [settings, setSettings] = useState<UserSettingsDraft>(() => readUserSettings(null))
  const [notifications, setNotifications] = useState<UserNotificationsPreference>(() => readNotifications(null))
  const seedNames = useCallback((u: StoredNames) => { setFirstName(u.first_name ?? ''); setLastName(u.last_name ?? ''); setPassword('') }, [])
  /* Only changed names; the password only when typed. */
  const nameChanges = (stored: StoredNames): Record<string, unknown> => ({
    ...(firstName !== (stored.first_name ?? '') ? { first_name: firstName } : {}),
    ...(lastName !== (stored.last_name ?? '') ? { last_name: lastName } : {}),
    ...(password ? { password } : {}),
  })
  return { firstName, setFirstName, lastName, setLastName, password, setPassword, seedNames, nameChanges,
    settings, setSettings, notifications, setNotifications }
}

/* First and last name (one row), then the new password; `children` sit between them (UserEditPage's Description). */
export function NameAndPasswordFields({ idPrefix, disabled, drafts, children }:
  { idPrefix: string; disabled: boolean; drafts: ReturnType<typeof useUserDrafts>; children?: ReactNode }) {
  return (
    <>
      {/* One row from 600px: two short fields side by side; stacked on a phone. `gap` matches the form's 1.75. */}
      <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr' }, gap: 1.75 }}>
        <FormTextField label="First name" id={`${idPrefix}-first-name`} disabled={disabled} type="text" value={drafts.firstName} onChange={drafts.setFirstName} />
        <FormTextField label="Last name" id={`${idPrefix}-last-name`} disabled={disabled} type="text" value={drafts.lastName} onChange={drafts.setLastName} />
      </Box>
      {children}
      <FormTextField
        label="Set new password" id={`${idPrefix}-password`} disabled={disabled} type="password" value={drafts.password}
        onChange={drafts.setPassword} placeholder="Leave blank to keep unchanged" autoComplete="new-password"
        slotProps={{ htmlInput: { minLength: PASSWORD_MIN } }}
      />
    </>
  )
}

/* The focused-project roster (`GET /api/projects`, grant-scoped to the viewer); a failure costs only that dropdown. */
export function useFocusableProjects(): { projects: FocusableProject[] | null; unavailable: boolean } {
  const roster = useApiRead(
    ['user-fields-projects'],
    () => listProjects().then(rows => (Array.isArray(rows) ? rows.map(p => ({ id: p.id, name: p.name })) : [])),
    'Failed to load projects',
  )
  return { projects: roster.data, unavailable: roster.error !== '' }
}
