import { useEffect, useState } from 'react'
import { FORM_ACTION_BAR_ATTR } from './FormActionBar'

/** Any page's form action bar (`FormActionBar.tsx` marks every one). */
const FORM_BAR_SELECTOR = `[${FORM_ACTION_BAR_ATTR}]`

const isDrawn = (bar: Element): boolean => bar.getClientRects().length > 0
const anyFormBarDrawn = (): boolean => Array.from(document.querySelectorAll(FORM_BAR_SELECTOR)).some(isDrawn)

/** Whether a page's form action bar is DRAWN right now — mounted is not enough: the task page keeps its form
 *  mounted in a `[hidden]` tab panel, and a bar laid out nowhere left Timeline and Details with no guide bar
 *  and the Help launcher lifted for nothing. Bars are found as they mount (a body observer, which also sees a
 *  panel's `hidden` flip) and watched for size. */
export function useFormBarShown(): boolean {
  // Read at once, so a page that opens with its form up never flashes the bar for a frame.
  const [shown, setShown] = useState(anyFormBarDrawn)
  useEffect(() => {
    let frame = 0
    const watched = new Set<Element>()
    const sizes = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => schedule())
    const measure = () => {
      frame = 0
      const bars = Array.from(document.querySelectorAll(FORM_BAR_SELECTOR))
      bars.forEach(bar => {
        if (!watched.has(bar)) {
          watched.add(bar)
          sizes?.observe(bar)
        }
      })
      watched.forEach(bar => {
        if (!bar.isConnected) {
          watched.delete(bar)
          sizes?.unobserve(bar)
        }
      })
      setShown(bars.some(isDrawn))
    }
    const schedule = () => { if (frame === 0) frame = requestAnimationFrame(measure) }
    measure()
    const mounts = new MutationObserver(schedule)
    // A tab panel shown or hidden by its `hidden` attribute, too: the size watch alone misses one without layout.
    mounts.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['hidden'] })
    return () => {
      cancelAnimationFrame(frame)
      mounts.disconnect()
      sizes?.disconnect()
    }
  }, [])
  return shown
}
