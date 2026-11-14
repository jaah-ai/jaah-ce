/* ERROR TRACKING WITH SENTRY, OFF UNTIL THE OPERATOR SETS A DSN. The DSN is the `error_tracking_dsn_web` operator
 * setting, served signed out by `GET /api/public/settings` (read through the login page's own query, so the page
 * pays no second request). Blank, `installErrorTracking` returns before the dynamic import: the Sentry chunk is
 * never fetched and nothing leaves the tab. Set, the SDK loads and reports uncaught errors and rejections with
 * the release (`APP_VERSION`), every event passing `scrubEvent` first (`lib/errorTrackingScrub.ts`).
 * Why and what is scrubbed: `apps/api/app/error_tracking.py`. */
import { scrubEvent } from './errorTrackingScrub'
import { APP_VERSION } from '../version'

/** The Sentry SDK, behind a seam so tests see whether it was loaded. */
export const loadSentry = { load: () => import('./errorTrackingSdk') }

/** Start Sentry for this tab with `dsn`; blank or absent does nothing. Never throws. True when started. */
export async function installErrorTracking(dsn: string | null | undefined): Promise<boolean> {
  const trimmed = (dsn ?? '').trim()
  if (!trimmed) return false
  try {
    const Sentry = await loadSentry.load()
    Sentry.init({
      dsn: trimmed,
      release: APP_VERSION,
      environment: window.location.hostname,
      // No user info, cookies, headers, bodies or query parameters collected; `scrubEvent` removes what is left.
      dataCollection: { userInfo: false, cookies: false, httpHeaders: false, httpBodies: [], urlQueryParams: false },
      // Errors only: no tracing option at all (any rate, even 0, turns tracing on), no replays or logs.
      defaultIntegrations: false,
      integrations: [
        Sentry.globalHandlersIntegration(),
        Sentry.linkedErrorsIntegration(),
        Sentry.dedupeIntegration(),
        Sentry.functionToStringIntegration(),
        Sentry.eventFiltersIntegration(),
      ],
      beforeSend: event => scrubEvent(event),
    })
    return true
  } catch {
    // Error tracking must never break the console.
    return false
  }
}
