/* THE HELP LAUNCHER'S SEAT: the room a scrollport keeps for the floating launcher in its corner
 * (`components/support/HelpLauncher.tsx`), and the hooks that keep a form action bar or a dock clear of it. Here, not
 * with the launcher, because every page's layout reads it: the community edition's tree ships no launcher
 * (`support_assistant`, packages/editions/features.toml), and its pages lay out the same. */
import { useEffect, type RefObject } from 'react'
import { ABOVE_PHONE, SECTION_FILL_ATTR, SHELL_FILL_ATTR } from './listStyles'
import { FORM_ACTION_BAR_ATTR } from './FormActionBar'

/** The launcher's diameter. */
export const LAUNCHER_SIZE = 56
/** Gap to the viewport's right and bottom edges. */
export const LAUNCHER_INSET = 24
/** The gap the clearance leaves between the launcher and the scrollport's content. */
const CLEARANCE_GAP = 8
/** The launcher's clearance in a scrollport it floats over (`App.tsx`'s `.app-main`, the list-detail pane's body):
 *  room for the launcher, its inset and a gap — reserved only where the launcher sits, never as a full-height
 *  gutter (a right gutter cost every table 56px). Bottom, so a list's last row (its `⋯`) scrolls clear
 *  of the corner; a page with a form action bar has the launcher level with that bar, and a fill page
 *  (`SHELL_FILL_ATTR`) scrolls inside itself: both keep the scrollport's own 1.75rem (a bar that does not end the
 *  content gives the rest back, `LAUNCHER_CLEAR_ATTR`). Right, only on that bar (`LAUNCHER_SEAT_VAR`), so its last button ends clear
 *  of the launcher beside it. No phone: the launcher is not drawn there. */
export const LAUNCHER_CLEARANCE_PX = LAUNCHER_INSET + LAUNCHER_SIZE + CLEARANCE_GAP
/** A form action bar's right-hand room for the launcher level with it, less the scrollport's own 2rem. Save keeps
 *  the orb's own LAUNCHER_INSET from it, not `CLEARANCE_GAP`, which put it almost under the orb's glow. */
const LAUNCHER_SEAT_PX = LAUNCHER_INSET + LAUNCHER_SIZE + LAUNCHER_INSET - 32
/** The same seat with no right inset under it; the list-detail pane's body takes its own inset off. */
export const LAUNCHER_SEAT_FLUSH_PX = LAUNCHER_INSET + LAUNCHER_SIZE + LAUNCHER_INSET
/** The seat a scrollport gives its form action bar; a scrollport off the launcher's corner sets it to 0. */
export const LAUNCHER_SEAT_VAR = '--launcher-seat'
/** On the detail pane's scrollport while its form action bar reaches the launcher's row. Only then does the launcher
 *  sit level with the bar, in the bar's seat (`LAUNCHER_SEAT_VAR`): a short form's bar sits mid-pane, where the seat
 *  only left Save short of the content's right edge. */
export const BAR_SEAT_ATTR = 'data-bar-seat'
/** On the detail pane's scrollport while it holds a form action bar but no drawn one ENDS its content: the bar sits in
 *  a hidden tab panel, or a card follows it (a user's Account under Profile). The bar's 1.75rem then left that content's
 *  last line under the orb at the pane's foot, so a spacer after the content restores the clearance: a spacer, not
 *  padding, which would lift a stuck bar off the scrollport's foot. Never with a fill page, which scrolls inside itself. */
export const LAUNCHER_CLEAR_ATTR = 'data-launcher-clear'
export const LAUNCHER_CLEARANCE_SX = {
  [ABOVE_PHONE]: {
    paddingBottom: `${LAUNCHER_CLEARANCE_PX}px`,
    [`&:has([${FORM_ACTION_BAR_ATTR}]), &:has([${SHELL_FILL_ATTR}])`]: { paddingBottom: '1.75rem' },
    [`&[${LAUNCHER_CLEAR_ATTR}]::after`]: { content: '""', display: 'block', height: `calc(${LAUNCHER_CLEARANCE_PX}px - 1.75rem)` },
    /* A fill page's tab panel with no fill section of its own (a task's Details) has nothing to scroll inside: it
       shrank to the page, its last lines spilling into the scrollport's padding under the orb, out of scroll's reach.
       Kept whole, it overflows the page so the scrollport scrolls it, and a spacer after it clears the orb: the whole
       clearance, as no padding follows overflow. A panel with a form bar seats the launcher level with the bar instead. */
    [`& [${SHELL_FILL_ATTR}] > [role="tabpanel"]:not([hidden]):not(:has([${SECTION_FILL_ATTR}], [${FORM_ACTION_BAR_ATTR}]))`]: {
      flexShrink: 0,
      '&::after': { content: '""', display: 'block', flexShrink: 0, height: `${LAUNCHER_CLEARANCE_PX}px` },
    },
    [LAUNCHER_SEAT_VAR]: `${LAUNCHER_SEAT_PX}px`,
    // `.app-main` beside an open detail pane is off the launcher's corner.
    '&:has(~ [data-testid="detail-pane"])': { [LAUNCHER_SEAT_VAR]: '0px' },
    [`& [${FORM_ACTION_BAR_ATTR}]`]: { paddingRight: `var(${LAUNCHER_SEAT_VAR})` },
  },
} as const
/** Bottom offset that centres the launcher on a stuck form action bar: the scrollport's 28px bottom inset, centred
 *  on the bar's 40px row. The page's `.app-main` and the detail pane's body share both. */
export const BAR_SEAT_BOTTOM = 20

/** Whether anything drawn follows `bar` inside `scrollport`: a later sibling of the bar or of any box between them. */
function trailed(bar: Element, scrollport: Element): boolean {
  for (let box: Element | null = bar; box && box !== scrollport; box = box.parentElement) {
    for (let next = box.nextElementSibling; next; next = next.nextElementSibling) {
      if (next.getClientRects().length > 0) return true
    }
  }
  return false
}

/** Keeps `BAR_SEAT_ATTR` and `LAUNCHER_CLEAR_ATTR` on `scrollport` true to where its bars are: re-measured on scroll, a
 *  window or pane resize, and any change to what it holds (a tab switch toggles `hidden`, a row is added). */
export function useBarSeat(scrollport: RefObject<HTMLElement | null>, active: boolean): void {
  useEffect(() => {
    const el = scrollport.current
    if (!active || !el) return undefined
    // The launcher's band, at its corner or in the seat: a bar reaching into it would put Save under the orb.
    const band = Math.max(LAUNCHER_INSET, BAR_SEAT_BOTTOM) + LAUNCHER_SIZE
    let frame = 0
    const measure = () => {
      frame = 0
      const bar = el.querySelector(`[${FORM_ACTION_BAR_ATTR}]`)
      el.toggleAttribute(BAR_SEAT_ATTR, bar !== null && bar.getBoundingClientRect().bottom > window.innerHeight - band)
      const bars = Array.from(el.querySelectorAll(`[${FORM_ACTION_BAR_ATTR}]`))
      el.toggleAttribute(LAUNCHER_CLEAR_ATTR, bars.length > 0 && el.querySelector(`[${SHELL_FILL_ATTR}]`) === null
        && !bars.some(b => b.getClientRects().length > 0 && !trailed(b, el)))
    }
    const schedule = () => { if (frame === 0) frame = requestAnimationFrame(measure) }
    measure()
    el.addEventListener('scroll', schedule, { passive: true })
    window.addEventListener('resize', schedule)
    const changes = new MutationObserver(schedule)
    changes.observe(el, { childList: true, subtree: true, attributes: true, attributeFilter: ['hidden'] })
    // The divider or Maximize reflows the form without a scroll, a window resize or a mutation.
    const resizes = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule)
    resizes?.observe(el)
    return () => {
      cancelAnimationFrame(frame)
      el.removeEventListener('scroll', schedule)
      window.removeEventListener('resize', schedule)
      changes.disconnect()
      resizes?.disconnect()
    }
  }, [scrollport, active])
}
/** On `<html>` while a dock sits in the launcher's corner (`useLauncherYield`): the launcher is then hidden. */
export const LAUNCHER_YIELD_ATTR = 'data-launcher-yield'

/** HIDES THE LAUNCHER WHILE `dock` SITS IN ITS CORNER, as it hides for a docked console: lifted just above
 *  the board's bulk dock, the orb still covered the last visible row's pick box and `⋯` mid-pick, and Help
 *  stays in the Sidebar. Re-measured as the dock grows or the window or a split pane moves it; a dock off the corner
 *  (beside an open pane) leaves the launcher alone. */
export function useLauncherYield(dock: RefObject<HTMLElement | null>, active: boolean): void {
  useEffect(() => {
    const el = dock.current
    if (!active || !el) return undefined
    const root = document.documentElement
    let frame = 0
    const measure = () => {
      frame = 0
      const rect = el.getBoundingClientRect()
      root.toggleAttribute(LAUNCHER_YIELD_ATTR, rect.height > 0 && rect.right > window.innerWidth - (LAUNCHER_INSET + LAUNCHER_SIZE + LAUNCHER_INSET))
    }
    const schedule = () => { if (frame === 0) frame = requestAnimationFrame(measure) }
    measure()
    window.addEventListener('resize', schedule)
    const resizes = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule)
    resizes?.observe(el)
    return () => {
      cancelAnimationFrame(frame)
      window.removeEventListener('resize', schedule)
      resizes?.disconnect()
      root.removeAttribute(LAUNCHER_YIELD_ATTR)
    }
  }, [dock, active])
}
