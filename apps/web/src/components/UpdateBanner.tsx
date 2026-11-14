import { useEffect, useRef, type JSX } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import { Banner } from '../ui/Banner'
import { tapTargetSx } from '../ui/listStyles'
import { useVersionCheck } from '../hooks/useVersionCheck'
import { hasUnsavedWork } from '../hooks/useLeaveGuard'
import { APP_VERSION } from '../version'

/* The console's ONE "new version" prompt, mounted once in `App.tsx::AuthLayout` above every page — and so the app's
 * one `useVersionCheck()` call. Never in the Live Cast viewer.
 *
 * A tab left on an old bundle reloads itself once it has been untouched this long (a hidden tab is untouched: no
 * input reaches it; moving the pointer, and coming back into view, are activity) and holds no unsaved work: a dirty `useLeaveGuard` page, text in
 * any text box or editable area (a comment composer keeps its draft in component state alone), or a focused field
 * with text in it. Blocked, it asks again on the next check. It reloads at most once per running build and at most
 * `MAX_AUTO_RELOADS` times per tab session, so neither a reload that lands on the same bundle (a stale cache) nor a
 * rollout serving two builds can bounce a tab; Reload by hand always works. */

export const IDLE_RELOAD_MS = 10 * 60_000
/** How often an idle tab with a known update re-asks whether it may reload. */
export const RELOAD_CHECK_MS = 30_000
/** sessionStorage: the build this tab last auto-reloaded FROM. */
export const RELOADED_FROM_KEY = 'jaah.update-reloaded-from'
/** sessionStorage: how many times this tab has auto-reloaded. */
export const AUTO_RELOADS_KEY = 'jaah.update-auto-reloads'
export const MAX_AUTO_RELOADS = 2

const ACTIVITY_EVENTS = ['pointerdown', 'pointermove', 'keydown', 'wheel', 'touchstart', 'scroll'] as const

const NON_TEXT_INPUTS = new Set(['button', 'checkbox', 'color', 'file', 'hidden', 'image', 'radio', 'range', 'reset', 'submit'])

/** True when the page holds text no guard has seen: any non-empty text box or editable area, or a focused text field
 *  (an unfocused `<input>` is more often a filter or a saved value than a draft). */
function typingInProgress(): boolean {
  for (const area of document.querySelectorAll('textarea')) if (area.value.trim() !== '') return true
  for (const area of document.querySelectorAll<HTMLElement>('[contenteditable]:not([contenteditable="false"])')) {
    if ((area.textContent ?? '').trim() !== '') return true
  }
  const el = document.activeElement
  if (el instanceof HTMLTextAreaElement) return el.value !== ''
  if (el instanceof HTMLInputElement) return !NON_TEXT_INPUTS.has(el.type) && el.value !== ''
  if (el instanceof HTMLElement && el.isContentEditable) return (el.textContent ?? '').trim() !== ''
  return false
}

/** True when this tab already auto-reloaded from this build, or as often as it may. */
function reloadSpent(): boolean {
  try {
    const count = Number(window.sessionStorage.getItem(AUTO_RELOADS_KEY)) || 0
    return window.sessionStorage.getItem(RELOADED_FROM_KEY) === APP_VERSION || count >= MAX_AUTO_RELOADS
  } catch {
    return false
  }
}

function markReloaded(): void {
  try {
    window.sessionStorage.setItem(RELOADED_FROM_KEY, APP_VERSION)
    const count = Number(window.sessionStorage.getItem(AUTO_RELOADS_KEY)) || 0
    window.sessionStorage.setItem(AUTO_RELOADS_KEY, String(count + 1))
  } catch {
    // No storage: the per-mount flag alone stops a second reload.
  }
}

const reloadPage = () => window.location.reload()
const wallClock = () => Date.now()

export function UpdateBanner({
  reload = reloadPage,
  now = wallClock,
}: {
  /** Injectable for tests. */
  reload?: () => void
  now?: () => number
} = {}): JSX.Element | null {
  const updateAvailable = useVersionCheck()
  const lastActive = useRef(now())
  const reloaded = useRef(false)

  useEffect(() => {
    const touch = () => { lastActive.current = now() }
    for (const name of ACTIVITY_EVENTS) window.addEventListener(name, touch, { capture: true, passive: true })
    return () => {
      for (const name of ACTIVITY_EVENTS) window.removeEventListener(name, touch, { capture: true })
    }
  }, [now])

  useEffect(() => {
    if (!updateAvailable) return
    const tryReload = () => {
      if (reloaded.current || reloadSpent()) return
      if (now() - lastActive.current < IDLE_RELOAD_MS) return
      if (hasUnsavedWork() || typingInProgress()) return
      reloaded.current = true
      markReloaded()
      reload()
    }
    // Coming back into view is activity: the reload happens while hidden, never as the user looks.
    const onVisibility = () => {
      if (document.visibilityState === 'visible') lastActive.current = now()
      else tryReload()
    }
    tryReload()
    const timer = window.setInterval(tryReload, RELOAD_CHECK_MS)
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [updateAvailable, now, reload])

  if (!updateAvailable) return null
  return (
    <Box data-testid="update-banner" sx={{ mb: 1.5 }}>
      <Banner
        severity="info"
        role="status"
        action={(
          <Button
            size="small"
            onClick={() => reload()}
            data-testid="update-reload"
            sx={[tapTargetSx, { whiteSpace: 'nowrap' }]}
          >
            Reload
          </Button>
        )}
      >
        New version available.
      </Banner>
    </Box>
  )
}

export default UpdateBanner
