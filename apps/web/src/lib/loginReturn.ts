import { readJson, removeKey, writeJson } from './safeStorage'

/* WHERE A SIGNED-OUT ARRIVAL WAS GOING (else a `/tasks/<id>` link opened in a signed-out tab lands on the
 * board). `App.tsx::RequireAuth` remembers the URL it bounces to `/login`; every sign-in door (password, Google,
 * a redirect provider's callback, MFA) ends on `/`, where `App.tsx::HomeRoute` returns to it once (beside
 * `lib/invitationReturn.ts`, which owns the invitation link's own return).
 * This tab's sessionStorage, so it survives a provider's round trip and no other tab ever reads it. Bounded in age,
 * so an abandoned sign-in does not hijack a much later visit to `/`. */

const STORAGE_KEY = 'jaah.login-return'
const MAX_AGE_MS = 30 * 60 * 1000

/* Same-origin console paths only (not `//host` nor `/\host`), and never the home or a sign-in page itself. */
const worthReturningTo = (path: string): boolean =>
  /^\/(?![/\\])/.test(path) && path !== '/' && !/^\/(login|logout)(\/|\?|#|$)/.test(path)

/* Set by an explicit sign-out (`auth.tsx::logout`): the page left behind is not where the NEXT person was going. */
let signedOut = false

/** The return is under way (or done): forget it, and remember again from here on. */
export function clearLoginReturn(): void {
  signedOut = false
  removeKey('session', STORAGE_KEY)
}

/** Sign-out was asked for: forget any remembered path and remember none until the next sign-in lands. */
export function forgetLoginReturn(): void {
  signedOut = true
  removeKey('session', STORAGE_KEY)
}

/** Remember `path` as where this tab was going when it was sent to sign in. */
export function rememberLoginReturn(path: string): void {
  if (!signedOut && worthReturningTo(path)) writeJson('session', STORAGE_KEY, { path, at: Date.now() })
}

/** The remembered path, or null when none is fresh. Reads only (a render may run twice): `clearLoginReturn`
 *  forgets it once the return is under way. */
export function loginReturn(now: number = Date.now()): string | null {
  const stored = readJson('session', STORAGE_KEY) as { path?: unknown; at?: unknown } | null
  if (stored === null) return null
  const { path, at } = stored ?? {}
  if (typeof path !== 'string' || typeof at !== 'number' || now - at > MAX_AGE_MS || at > now) return null
  return worthReturningTo(path) ? path : null
}
