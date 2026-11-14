// THE PREFERENCES DOCUMENT A SETTINGS SAVE SENDS, for both surfaces that edit a user's preferences — `/profile`
// (`PUT /api/auth/me`) and a superadmin's `/users/:id` (`PATCH /api/users/{id}`). Both endpoints replace
// `preferences` WHOLESALE and `userSettingsToPreferences` is PARTIAL, so the settings keys ride
// `mergedUserPreferences` (every draft key written, which also repairs a legacy value `readUserSettings` degraded);
// `task_rail_pins` and whatever else is stored survive. Alerts are not preferences: they are notification channels,
// saved by `components/NotificationsPanel` itself, so a stale `notifications` block is never sent.
import { mergedUserPreferences, readUserSettings, userSettingsToPreferences, type UserSettingsDraft } from './userSettings'

/** The document to send, or `null` when the settings did not change (the body then says nothing about preferences).
 *  Compared key-wise over the draft's own keys (both sides built from the same exhaustive map). */
export function preferencesPatch(
  stored: Record<string, unknown> | null | undefined,
  settings: UserSettingsDraft,
): Record<string, unknown> | null {
  const next = userSettingsToPreferences(settings)
  const current = userSettingsToPreferences(readUserSettings(stored))
  if (!Object.keys(next).some(key => next[key] !== current[key])) return null
  const { notifications: _gone, ...preferences } = mergedUserPreferences(stored, settings)
  return preferences
}
