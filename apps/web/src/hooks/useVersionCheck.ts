import { useQuery } from '@tanstack/react-query'
import { APP_VERSION } from '../version'

// Detects a newer web bundle deployed under a running tab — nothing else re-fetches the
// executing JS. Polls the static manifest (`dist/version.json` from vite.config.ts's plugin, no-cache under
// nginx `location /`), not the API, which deploys independently.

const POLL_INTERVAL_MS = 60_000

// Ignores the build's `+sha` or `+dirty` suffix: a same-commit redeploy is not an update.
function bareVersion(version: string): string {
  return version.split('+')[0]
}

export const DEPLOYED_VERSION_KEY = ['deployed-version'] as const

// Vite's un-injected '0.0.0' fallback is not a real build; nothing to compare.
export const VERSION_CHECKED = bareVersion(APP_VERSION) !== '0.0.0'

async function fetchDeployedVersion(): Promise<string | null> {
  try {
    const resp = await fetch('/version.json', { cache: 'no-store' })
    if (!resp.ok) return null
    const data: unknown = await resp.json()
    if (!data || typeof data !== 'object') return null
    const version = (data as { version?: unknown }).version
    return typeof version === 'string' ? version : null
  } catch {
    // Network error / not deployed yet — stay quiet, try again next tick.
    return null
  }
}

/** True when the deployed bundle differs from this tab's; false when it matches or cannot be read. The query
 *  function behind `DEPLOYED_VERSION_KEY`, so a one-off check (`UnknownRoute`) seeds the Sidebar's answer. */
export async function newerVersionDeployed(): Promise<boolean> {
  const deployed = await fetchDeployedVersion()
  return deployed !== null && bareVersion(deployed) !== bareVersion(APP_VERSION)
}

export function useVersionCheck(): boolean {
  const { data } = useQuery({
    queryKey: DEPLOYED_VERSION_KEY,
    queryFn: newerVersionDeployed,
    // Sticky: once an update is seen nothing re-checks, so a later 404 or rollback never hides the prompt.
    enabled: query => query.state.data !== true && VERSION_CHECKED,
    refetchInterval: POLL_INTERVAL_MS,
    refetchOnWindowFocus: 'always',
  })
  return data === true
}
