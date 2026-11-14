// THE USER-SETTINGS FIELD SET, AND THE ONE PLACE IT IS ENUMERATED. Two surfaces edit a user's
// preferences — `/profile` and a superadmin's `/users/:id` Settings tab — and must offer the SAME set.
// Single-homed here:
//   - `UserSettingsDraft` — the field set. `tsc` forces a new field to be READ (`readUserSettings`) and
//     WRITTEN (`STORED_KEYS`); it cannot force it RENDERED — the field-census tests and
//     `e2e/settings-parity.spec.ts` hold that.
//   - `readUserSettings` — stored preferences → draft.
//   - `userSettingsToPreferences` — draft → the wire keys, identical for both surfaces.
// The controls live once in `components/UserSettingsFields.tsx`. Pure of React: the pages own WHEN to
// read/write and the endpoint (`PUT /api/auth/me` for self, `PATCH /api/users/{id}` for a superadmin).

/**
 * Every preference either settings surface offers, and nothing else.
 * Absent: alerts — notification channels, which `components/NotificationsPanel.tsx` saves on its own.
 */
export type UserSettingsDraft = {
  /** `'default'` = never picked: the operator's default applies (`lib/colorScheme.ts`); stored absent. */
  theme: 'default' | 'light' | 'dark'
}

/**
 * A draft seeded from stored preferences — the ONE hydrate rule. Each field degrades to a defined
 * default.
 */
export function readUserSettings(
  preferences: Record<string, unknown> | null | undefined,
): UserSettingsDraft {
  return {
    theme: preferences?.theme === 'dark' || preferences?.theme === 'light' ? preferences.theme : 'default',
  }
}

/* One stored key per draft field, as an EXHAUSTIVE map keyed on `keyof UserSettingsDraft`: a plain
 * literal with a `Record<string, unknown>` return let a new field type-check while being saved nowhere.
 * Each entry pairs the stored key with its wire translation. */
const STORED_KEYS: {
  [K in keyof UserSettingsDraft]: readonly [key: string, toWire: (value: UserSettingsDraft[K]) => unknown]
} = {
  /* `undefined`, not `null`: JSON drops the key, which the server reads as unset, and an outgoing API release
     during a rollout accepts an absent key but 422s a `null` (`preferences.py::preferences_document`). */
  theme: ['theme', value => (value === 'default' ? undefined : value)],
}

/**
 * The draft as the server's preference keys — the ONE write shape.
 * PARTIAL by design: both write paths REPLACE `admin_users.preferences` wholesale (`routes/auth.py`,
 * `routes/users.py`), so callers merge it over the stored document (`mergedUserPreferences`) or they
 * wipe the task rail's keys.
 */
export function userSettingsToPreferences(draft: UserSettingsDraft): Record<string, unknown> {
  const stored: Record<string, unknown> = {}
  for (const field of Object.keys(STORED_KEYS) as (keyof UserSettingsDraft)[]) {
    // One `as`: TS cannot see that `STORED_KEYS[field]`'s writer and `draft[field]` share `K` once
    // `Object.keys` widens `field`. The mapped type guarantees it.
    const [key, toWire] = STORED_KEYS[field] as readonly [string, (value: unknown) => unknown]
    stored[key] = toWire(draft[field])
  }
  return stored
}

/**
 * The COMPLETE preferences document to send: these keys merged over everything they do not own (both
 * write paths replace wholesale). Serves the two draft-shaped callers (`lib/preferencesPatch.ts`). Takes the stored document rather
 * than reading one: the surfaces hold it in different places.
 */
export function mergedUserPreferences(
  stored: Record<string, unknown> | null | undefined,
  draft: UserSettingsDraft,
): Record<string, unknown> {
  return { ...(stored ?? {}), ...userSettingsToPreferences(draft) }
}
