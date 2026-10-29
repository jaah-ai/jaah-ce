/* Which colour scheme a page paints, signed in or out — THE ONE RULE, read by `auth.tsx` (which writes `<html data-theme>`)
 * and by every surface that must paint to match (`hooks/useColorMode.ts`).
 * The user's own `preferences.theme` wins; unset (never picked) follows the operator's `default_theme` knob,
 * delivered as `admin.default_theme` (`routes/auth.py::AdminOut.default_theme`). Signed out: the browser's
 * `prefers-color-scheme`. */
import type { Admin } from '../auth'

export type ColorScheme = 'light' | 'dark'

/** The default until the server answers (an old snapshot, a missing field) — duplicates the catalogue default. */
export const FALLBACK_DEFAULT_THEME: ColorScheme = 'dark'

/** The operator's default, or the fallback for a value this build does not recognise. */
export function resolveDefaultTheme(raw: unknown): ColorScheme {
  return raw === 'light' || raw === 'dark' ? raw : FALLBACK_DEFAULT_THEME
}

/** The query a signed-out page follows; `auth.tsx` repaints when it flips. */
export const SYSTEM_DARK_QUERY = '(prefers-color-scheme: dark)'

/** The browser's own scheme; light where the browser cannot say. */
export function systemColorScheme(): ColorScheme {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return 'light'
  return window.matchMedia(SYSTEM_DARK_QUERY).matches ? 'dark' : 'light'
}

/** Call `onChange` whenever the browser's scheme flips; returns the unsubscribe. */
export function subscribeSystemColorScheme(onChange: () => void): () => void {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return () => {}
  const query = window.matchMedia(SYSTEM_DARK_QUERY)
  query.addEventListener?.('change', onChange)
  return () => query.removeEventListener?.('change', onChange)
}

/** The scheme `admin` sees. */
export function colorSchemeFor(admin: Pick<Admin, 'preferences' | 'default_theme'> | null | undefined): ColorScheme {
  if (!admin) return systemColorScheme()
  const own = admin.preferences?.theme
  return own === 'light' || own === 'dark' ? own : resolveDefaultTheme(admin.default_theme)
}
