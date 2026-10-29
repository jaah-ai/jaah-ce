/* Press, move, release — the drag gesture for any list. A pointer drag, not HTML5 DnD (argued in
 * `components/LayoutDesigner.tsx`'s header): jsdom cannot drive DnD. Window-level
 * `pointermove`/`pointerup`; no `setPointerCapture` (jsdom lacks it).
 * The destination is resolved from the pointer on every move (`event.target`), never accumulated from
 * row `pointerenter` handlers: that approach turned a release outside the list, a drop back onto the
 * `pointer-events: none` source row, handler-less rows, and a threshold crossed inside the destination
 * into wrong writes.
 * Owns: the movement threshold, the destination, the release, every non-release ending, and the one
 * click to swallow. The caller owns what may be lifted, what is a destination, and what a drop writes
 * (`resolve`, `onDrop`).
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import type { PointerEvent as ReactPointerEvent } from 'react'

/** Travel before a press becomes a drag; above click tremor, like the native drag threshold. */
const DRAG_THRESHOLD_PX = 5

/** How long the click swallow stays armed. A release's click arrives in the same task; this only bounds
 *  releases that produce no click (over browser chrome, another window, an unmounted row). */
const CLICK_SWALLOW_MS = 400

export type PointerDragOptions<Item, Target> = {
  /**
   * What is under the pointer, as a destination, or `null`. Called on every move, so it must be cheap and
   * pure (`element.closest('[data-…]')`). Compared with `Object.is`, so return a primitive key: a fresh
   * object re-renders the list on every pixel.
   */
  resolve: (element: Element | null) => Target | null
  /** Perform the drop. Called once, on release, only with a destination `resolve` returned. */
  onDrop: (item: Item, target: Target) => void
}

export type PointerDrag<Item, Target> = {
  /** The item in flight, or `null`. A press below the threshold is still a click, not a drag. */
  item: Item | null
  /** The destination under the pointer right now, or `null`. */
  target: Target | null
  /** Begin a possible drag. Wire it to a row's pointer-down. */
  lift: (item: Item, event: ReactPointerEvent<HTMLElement>) => void
}

export function usePointerDrag<Item, Target>(
  options: PointerDragOptions<Item, Target>,
): PointerDrag<Item, Target> {
  const [item, setItem] = useState<Item | null>(null)
  const [target, setTarget] = useState<Target | null>(null)

  /* The press not yet a drag. A ref: recording it must not re-render. */
  const pending = useRef<{ item: Item; x: number; y: number } | null>(null)
  /* Read by the window listeners, so the effect is armed once, not on every move. */
  const active = useRef<Item | null>(null)
  const over = useRef<Target | null>(null)
  /* A drag happened and its click has not arrived. Separate from `active` because Escape abandons a drag
  with the button still down, so the click still fires (and would follow the row's title link). Set only
  from Escape: every `bail()` means the button is already up. Disarmed three ways via
  `armFlag`/`disarmFlag`. */
  const swallowNext = useRef(false)
  /* Which pointer this drag belongs to; otherwise a second pointer's `pointerup` would commit the drop. */
  const pointerId = useRef<number | null>(null)
  /* Live callbacks, assigned in an effect: a ref write during render leaks from abandoned concurrent
  renders. */
  const opts = useRef(options)
  useEffect(() => {
    opts.current = options
  })

  const endDrag = useCallback(() => {
    pending.current = null
    active.current = null
    over.current = null
    pointerId.current = null
    setItem(null)
    setTarget(null)
  }, [])

  useEffect(() => {
    /* ── The click a completed drag produces, and its three disarms ──
    A drag starting on a row's title link ends with a `click` on it, so the next click is swallowed in the
    capture phase. Disarmed when it fires, on a timeout, and in cleanup: a release over browser chrome
    produces no click, and a once-only listener would eat the next unrelated click (even in a dialog).
    `stopPropagation`, not `stopImmediatePropagation`: other window capture listeners are not ours. */
    let disarmTimer: number | undefined
    let flagTimer: number | undefined

    function disarmSwallow() {
      window.clearTimeout(disarmTimer)
      window.removeEventListener('click', swallow, { capture: true })
    }

    /* The flag needs the same three disarms: its release may never be delivered, and a stale flag would arm
    the swallow off the next unrelated release. */
    function armFlag() {
      window.clearTimeout(flagTimer)
      swallowNext.current = true
      flagTimer = window.setTimeout(() => {
        swallowNext.current = false
      }, CLICK_SWALLOW_MS)
    }

    function disarmFlag() {
      window.clearTimeout(flagTimer)
      swallowNext.current = false
    }

    function swallow(event: Event) {
      event.preventDefault()
      event.stopPropagation()
      disarmSwallow()
    }

    function armSwallow() {
      disarmSwallow()
      window.addEventListener('click', swallow, { capture: true })
      disarmTimer = window.setTimeout(disarmSwallow, CLICK_SWALLOW_MS)
    }

    /** Ask the caller what is under the pointer; re-render only on change. The target is shape-checked:
     *  `Document`/`Window` have no `closest`, and a throw in `release` would skip teardown and leave the drag
     *  armed. */
    function retarget(eventTarget: EventTarget | null) {
      const next = opts.current.resolve(eventTarget instanceof Element ? eventTarget : null)
      if (Object.is(next, over.current)) return
      over.current = next
      setTarget(next)
    }

    /** Is this event part of the drag in flight? A second pointer's events are not. */
    function mine(event: PointerEvent) {
      return pointerId.current === null || event.pointerId === pointerId.current
    }

    /** A drag that ended with the button already up (no buttons on move, cancelled pointer, lost window).
     *  No click can follow, so none of these arms the swallow. */
    function bail() {
      disarmFlag()
      endDrag()
    }

    function move(event: PointerEvent) {
      if (!mine(event)) return
      /* The button came up where we never heard (browser chrome, another window, context menu). Without this
      check the drag stays armed and the next press's release would commit the old item. */
      if (event.buttons === 0) {
        if (pending.current || active.current) bail()
        return
      }

      const press = pending.current
      if (press && !active.current) {
        if (Math.hypot(event.clientX - press.x, event.clientY - press.y) < DRAG_THRESHOLD_PX) return
        active.current = press.item
        pending.current = null
        /* Drop the text selection press-and-move made: it survives the drop, and a feed row refuses presses
        while a selection is live. The caller suppresses further selection during the drag. */
        window.getSelection()?.removeAllRanges()
        setItem(press.item)
      }

      /* Resolved from this event too, so a threshold crossed inside the destination lands on it. */
      if (active.current) retarget(event.target)
    }

    function release(event: PointerEvent) {
      if (!mine(event)) return
      const dragged = active.current
      // One last resolve at the release point.
      if (dragged !== null) retarget(event.target)
      const destination = over.current
      const wasDrag = dragged !== null || swallowNext.current
      disarmFlag()
      endDrag()
      if (wasDrag) armSwallow()
      if (dragged !== null && destination !== null) opts.current.onDrop(dragged, destination)
    }

    function cancel(event: KeyboardEvent) {
      if (event.key !== 'Escape') return
      // The one abandon where the button is still down, so the click is still coming.
      if (active.current) armFlag()
      endDrag()
    }

    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', release)
    window.addEventListener('pointercancel', bail)
    window.addEventListener('blur', bail)
    window.addEventListener('keydown', cancel)
    return () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', release)
      window.removeEventListener('pointercancel', bail)
      window.removeEventListener('blur', bail)
      window.removeEventListener('keydown', cancel)
      disarmSwallow()
      disarmFlag()
    }
  }, [endDrag])

  const lift = useCallback(
    (next: Item, event: ReactPointerEvent<HTMLElement>) => {
      /* Primary button only (right opens a menu, middle pastes on X11). */
      if (event.button !== 0) return
      /* Mouse and pen only, deliberately: a touch drag needs `touch-action: none` on every row, which would
      make the list unscrollable on a phone. The verb has a second home in the row's menu. */
      if (event.pointerType === 'touch') return
      /* End any standing drag first; a stale `active` would make this release commit the previous item. */
      endDrag()
      pointerId.current = event.pointerId
      pending.current = { item: next, x: event.clientX, y: event.clientY }
    },
    [endDrag],
  )

  return { item, target, lift }
}
