/* Whether a person is at this console. The API's idle timeout (`apps/api/app/auth.py::SESSION_IDLE_TIMEOUT`)
 * must count only requests a person caused: a request sent after the quiet window with no input — a poll
 * in an unattended tab — carries `X-Activity: background`, and the server never re-mints the session for
 * it (`apps/api/app/login_funnel.py::SessionRefreshMiddleware`). The window is the server's refresh
 * interval: 15 minutes, or 60 seconds in a HIPAA org (`apps/api/app/hipaa.py::REFRESH_AFTER`), where a
 * 15-minute window would let a poll stretch the 15-minute idle logoff to 30. */
import { loadAuthSnapshot } from '../authStore'

export const BACKGROUND_AFTER_MS = 15 * 60 * 1000
export const HIPAA_BACKGROUND_AFTER_MS = 60 * 1000
export const ACTIVITY_HEADER = 'X-Activity'

/* The snapshot is the last `/api/auth/me` answer, shared by every tab; absent reads as an OFF org. */
function backgroundAfterMs(): number {
  return loadAuthSnapshot()?.org_hipaa ? HIPAA_BACKGROUND_AFTER_MS : BACKGROUND_AFTER_MS
}

let lastInputAt = Date.now()

export function markActivity(at: number = Date.now()): void {
  lastInputAt = at
}

if (typeof window !== 'undefined') {
  for (const type of ['pointerdown', 'keydown', 'wheel', 'touchstart']) {
    window.addEventListener(type, () => markActivity(), { capture: true, passive: true })
  }
}

/* `headers` unchanged while a person is active (the same object, so nothing else about the request
 * moves); otherwise a copy carrying the background marker. */
export function withActivity(headers: HeadersInit | undefined, now: number = Date.now()): HeadersInit | undefined {
  if (now - lastInputAt <= backgroundAfterMs()) return headers
  const merged = new Headers(headers)
  merged.set(ACTIVITY_HEADER, 'background')
  return Object.fromEntries(merged.entries())
}
