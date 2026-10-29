import { useLayoutEffect, useState, type RefObject } from 'react'

/** False while `ref`'s box has no width. A hidden tab is `display: none`: x-charts measures it 0 wide and lays its
 *  margins and axis out into a negative drawing area (`<rect width="-60">` on a horizontal bar chart), so a plot is
 *  drawn only while its box has width — a hidden tab's charts mount when the tab is shown. With no ResizeObserver
 *  (jsdom) it is always true. */
export function useHasWidth(ref: RefObject<HTMLElement | null>): boolean {
  const [has, setHas] = useState(() => typeof ResizeObserver === 'undefined')
  useLayoutEffect(() => {
    const el = ref.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const measure = () => setHas(el.getBoundingClientRect().width > 0)
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(el)
    return () => observer.disconnect()
  }, [ref])
  return has
}

/** `el`'s rendered width, tracked; `null` with no element yet or no ResizeObserver (jsdom). Takes the ELEMENT (a
 *  callback ref held in state), not a ref object: a box that mounts after the first render — a page's body once
 *  its record loads — is observed the moment it exists, which a `[ref]` effect would never re-run for. */
export function useElementWidth(el: HTMLElement | null): number | null {
  const [width, setWidth] = useState<number | null>(null)
  useLayoutEffect(() => {
    if (!el || typeof ResizeObserver === 'undefined') {
      setWidth(null)
      return
    }
    const measure = () => setWidth(Math.round(el.getBoundingClientRect().width))
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(el)
    return () => observer.disconnect()
  }, [el])
  return width
}
