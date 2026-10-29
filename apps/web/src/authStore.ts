// Untrusted display-only auth snapshot so a reload paints without flashing /login. Never a credential:
// the HttpOnly session cookie is, and the server re-validates every request.

import type { Admin } from './auth'
import { readJsonObject, writeJson } from './lib/safeStorage'

const KEY = 'cockpit.auth'
export const AUTH_EXPIRED_EVENT = 'cockpit:auth-expired'

export function loadAuthSnapshot(): Admin | null {
  const obj = readJsonObject('local', KEY)
  /* A NON-EMPTY STRING id. String: `Admin.id` is a uuid (a numeric id names a row that no longer exists,
   * so a pre-migration snapshot is discarded). Non-empty: `''` would be taken by `auth.tsx`'s
   * `cachedForIdRef` as a real id and the react-query cache would NOT be cleared when the real admin
   * resolved — one account reading another's data in a shared browser profile. */
  return obj && typeof obj.id === 'string' && obj.id !== '' && obj.email ? (obj as unknown as Admin) : null
}

export function saveAuthSnapshot(admin: Admin): void {
  writeJson('local', KEY, admin)
}

export function clearAuthSnapshot(): void {
  try {
    localStorage.removeItem(KEY)
  } catch {
    /* ignore */
  }
}
