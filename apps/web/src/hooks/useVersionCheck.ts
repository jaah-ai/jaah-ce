import { useQuery } from '@tanstack/react-query'
import { readJson } from '../api'
import { abortAfter, send } from '../net/send'
import { APP_VERSION } from '../version'

// Detects a newer web bundle deployed under a running tab — nothing else re-fetches the
// executing JS. Polls the static manifest (`dist/version.json` from vite.config.ts's plugin, no-cache under
// nginx `location /`), not the API, which deploys independently.

const POLL_INTERVAL_MS = 60_000
/** A check unanswered this long is "unknown", never a hang (`net/send.ts`'s bound). */
export const VERSION_CHECK_BOUND_MS = 10_000

// Ignores the build's `+sha` or `+dirty` suffix: a same-commit redeploy is not an update.
function bareVersion(version: string): string {
  return version.split('+')[0]
}

export const DEPLOYED_VERSION_KEY = ['deployed-version'] as const

// Vite's un-injected '0.0.0' fallback is not a real build; nothing to compare.
export const VERSION_CHECKED = bareVersion(APP_VERSION) !== '0.0.0'

/** The deployed version, or null when it cannot be read whole: no answer within the bound, a refusal, a cut body. */
async function fetchDeployedVersion(): Promise<string | null> {
  // The bound covers the body too: a connection cut after the headers must not hold the poll forever.
  const bound = abortAfter(VERSION_CHECK_BOUND_MS)
  try {
    // Raw: a static file, so no session headers and no relay (`net/send.ts`), only the bound.
    const resp = await send('/version.json', { cache: 'no-store', raw: true, signal: bound.signal })
    if (!resp.ok) return null
    const data = await readJson(resp, '/version.json')
    if (!data || typeof data !== 'object') return null
    const version = (data as { version?: unknown }).version
    return typeof version === 'string' ? version : null
  } catch {
    // No answer, a cut body, not deployed yet — stay quiet, try again next tick.
    return null
  } finally {
    bound.clear()
  }
}

/** True when the deployed bundle differs from this tab's; false when it matches or cannot be read. The query
 *  function behind `DEPLOYED_VERSION_KEY`, so a one-off check (`UnknownRoute`) seeds `UpdateBanner`'s answer. */
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
    // A hidden tab polls too, so the idle auto-reload (`components/UpdateBanner.tsx`) reaches a tab left in the
    // background; one small static file a minute.
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: 'always',
  })
  return data === true
}
