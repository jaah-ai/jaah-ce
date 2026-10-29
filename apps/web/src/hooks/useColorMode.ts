import { useSyncExternalStore } from 'react'
import { useAuth } from '../auth'
import { colorSchemeFor, subscribeSystemColorScheme, systemColorScheme } from '../lib/colorScheme'

/** The scheme on screen. `auth.tsx` writes `<html data-theme>` from this same preference; `palette.mode` is
 *  frozen under `cssVariables`, so it cannot answer which literal colour a chart (an SVG attribute) draws with. */
export function useColorMode(): 'light' | 'dark' {
  const { admin } = useAuth()
  // Signed out, the browser's scheme — re-read when it flips, so a mounted Google button repaints with it.
  const system = useSyncExternalStore(subscribeSystemColorScheme, systemColorScheme, () => 'light' as const)
  return admin ? colorSchemeFor(admin) : system
}
