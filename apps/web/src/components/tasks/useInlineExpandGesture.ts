import { useCallback, useRef } from 'react'
import type { MouseEvent as ReactMouseEvent, TouchEvent as ReactTouchEvent } from 'react'

/**
 * THE DOUBLE-PRESS THAT OPENS THE FULL EDITOR — a second way to the inline create row's ⤢ button, where
 * the caret already sits.
 *
 * Fires on the whole row, text box included (operator decision), except controls and opted-out prose —
 * hence `onRowMouseDown`'s word-select suppression (a pointer-path property; see it). Not on existing task
 * rows. Never a replacement for ⤢, which stays the KEYBOARD path (its tooltip, in `./useInlineTaskCreate`,
 * advertises the gesture).
 *
 * The create row (`./listViews/ActivityFeedView`) uses it: it returns a handler bundle to spread on the row.
 */

/* The double-press window: 400ms (not the browser's ~300ms) because a thumb is slower and `touchend`
   carries no `detail` count; short enough that focus-then-caret taps are not one gesture. */
export const DOUBLE_TAP_MS = 400

/* How far apart the two taps may land: 24px, inside one tap target. Exported so tests read the rule. */
export const DOUBLE_TAP_SLOP_PX = 24

/* What the gesture must not swallow: the row's single-press controls (▶, 💬, ⤢) — a retried ▶ must not
   open the editor — and `.MuiChip-root`, whose delete icon is an `<svg>` handled on the chip. */
const CONTROL_SELECTOR = 'button, a, [role="button"], .MuiChip-root'

/* The opt-out for PROSE about the row (its filing-scope caption and error line). Both surfaces carry it,
   though the feed's structure already excludes them — structure is a layout accident, not a guarantee.
   Spread `EXPAND_GESTURE_OFF` so the surfaces cannot drift. */
export const EXPAND_GESTURE_OFF = { 'data-expand-gesture': 'off' } as const
const OPT_OUT_SELECTOR = '[data-expand-gesture="off"]'

function outsideTheGesture(target: EventTarget | null): boolean {
  return (
    target instanceof Element && target.closest(`${CONTROL_SELECTOR}, ${OPT_OUT_SELECTOR}`) !== null
  )
}

/* `touch-action: manipulation`, or the phone's double-tap-to-zoom claims the second tap. Merged into each
   surface's row `sx` (this hook renders nothing) and asserted at both surfaces. */
export const EXPAND_GESTURE_SX = { touchAction: 'manipulation' } as const

export type InlineExpandGesture = {
  /* Named for the row: the gesture is the ROW's, not the input's. */
  onRowMouseDown: (event: ReactMouseEvent) => void
  onRowDoubleClick: (event: ReactMouseEvent) => void
  onRowTouchEnd: (event: ReactTouchEvent) => void
}

/**
 * `open` takes nothing (each surface closes over its own text, like its ⤢). Read through a ref so the
 * handlers stay stable (callers pass inline arrows).
 *
 * `disabled` mirrors ⤢'s `disabled={submitting}`: navigating mid-POST would abandon the write. Not wired to
 * the input's `noProject` disable: a disabled box dispatches no mouse events, while the row (like ⤢) still
 * answers.
 */
export function useInlineExpandGesture(open: () => void, disabled = false): InlineExpandGesture {
  const openRef = useRef(open)
  openRef.current = open
  // The first tap of a possible double-tap; `null` between gestures.
  const firstTapRef = useRef<{ at: number; x: number; y: number } | null>(null)
  // When it last fired: a recognised double-tap is followed by the browser's own synthesised click/dblclick,
  // which must not open the editor twice.
  const firedAtRef = useRef(0)
  /* Did either press hit a control? `dblclick` targets the presses' nearest COMMON ANCESTOR (the row, for
     ▶ then input), so each press's real target is recorded on `mousedown` and read at the end. */
  const pairHitControlRef = useRef(false)

  const fire = useCallback(() => {
    const now = Date.now()
    if (now - firedAtRef.current < DOUBLE_TAP_MS) return
    firedAtRef.current = now
    openRef.current()
  }, [])

  /* The word-select suppression lives here, not in `dblclick` (by then the input's own selection is set):
     preventing the SECOND mousedown's default (`detail >= 2`) stops it; `dblclick` still fires. Primary
     button only — rapid right-presses also count, and Firefox opens its context menu on `mousedown`. The
     touch path cannot suppress it (selection happens at the touch layer); navigating away makes it at
     worst a flash. */
  const onRowMouseDown = useCallback((event: ReactMouseEvent) => {
    if (event.button !== 0) return
    const onControl = outsideTheGesture(event.target)
    // `detail` 0/1 opens a fresh pair; higher adds to the current one.
    pairHitControlRef.current = event.detail <= 1 ? onControl : pairHitControlRef.current || onControl
    if (disabled || onControl || event.detail < 2) return
    event.preventDefault()
  }, [disabled])

  const onRowDoubleClick = useCallback((event: ReactMouseEvent) => {
    if (disabled || pairHitControlRef.current || outsideTheGesture(event.target)) return
    fire()
  }, [disabled, fire])

  const onRowTouchEnd = useCallback((event: ReactTouchEvent) => {
    if (disabled || outsideTheGesture(event.target)) return
    // One finger, the last to leave: multi-touch is not a tap.
    if (event.touches.length > 0 || event.changedTouches.length !== 1) return
    const touch = event.changedTouches[0]
    const now = Date.now()
    const first = firstTapRef.current
    const isSecondTap =
      first !== null &&
      now - first.at <= DOUBLE_TAP_MS &&
      Math.abs(touch.clientX - first.x) <= DOUBLE_TAP_SLOP_PX &&
      Math.abs(touch.clientY - first.y) <= DOUBLE_TAP_SLOP_PX
    if (isSecondTap) {
      // Cleared before firing so a third tap starts fresh — true by code, not by the two windows happening
      // to share a constant.
      firstTapRef.current = null
      fire()
      return
    }
    firstTapRef.current = { at: now, x: touch.clientX, y: touch.clientY }
  }, [disabled, fire])

  return { onRowMouseDown, onRowDoubleClick, onRowTouchEnd }
}
