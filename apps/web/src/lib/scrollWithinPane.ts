type Block = 'start' | 'nearest'

/** What ends `keepInPane`'s hold: the reader taking over the pane. */
const READER_EVENTS = ['wheel', 'touchstart', 'pointerdown', 'keydown'] as const

/** The nearest ancestor that scrolls vertically (the pane), or null where the document scrolls. */
export function scrollingAncestor(el: HTMLElement): HTMLElement | null {
  let pane = el.parentElement
  while (pane && !/auto|scroll/.test(getComputedStyle(pane).overflowY)) pane = pane.parentElement
  return pane
}

function scrollPane(pane: HTMLElement, el: HTMLElement, block: Block): void {
  // Less its `scroll-margin-top`, as the browser's own scrolling does: what a sticky bar over the pane would cover.
  const margin = parseFloat(getComputedStyle(el).scrollMarginTop) || 0
  const offset = el.getBoundingClientRect().top - pane.getBoundingClientRect().top
  const top = offset - margin
  if (block === 'start') {
    pane.scrollTop += top
    return
  }
  const below = offset + el.offsetHeight - pane.clientHeight
  if (top < 0) pane.scrollTop += top
  else if (below > 0) pane.scrollTop += Math.min(top, below)
}

/** Brings `el` into view by scrolling only its nearest scrolling ancestor (the pane). `scrollIntoView` scrolls
 * every ancestor, `overflow: hidden` ones included, so a card near a pane's end slid the whole app shell and left a
 * blank band. With no scrolling ancestor (a phone, where the document scrolls) it falls back to `scrollIntoView`. */
export function scrollWithinPane(el: HTMLElement, block: Block): void {
  const pane = scrollingAncestor(el)
  if (pane) scrollPane(pane, el, block)
  else el.scrollIntoView?.({ block })
}

/** `scrollWithinPane`, then held there for up to `ms` while the pane's other cards load and push `el` down —
 * a deep link fires before they draw. The reader scrolling, clicking or typing ends the hold at once. Returns the
 * stop, for an effect's cleanup. */
export function keepInPane(el: HTMLElement, block: Block, ms = 3000): () => void {
  const pane = scrollingAncestor(el)
  if (!pane) {
    el.scrollIntoView?.({ block })
    return () => {}
  }
  let frame = 0
  const until = performance.now() + ms
  const stop = () => {
    cancelAnimationFrame(frame)
    for (const type of READER_EVENTS) pane.removeEventListener(type, stop)
  }
  const hold = () => {
    if (!el.isConnected || performance.now() > until) return stop()
    scrollPane(pane, el, block)
    frame = requestAnimationFrame(hold)
  }
  for (const type of READER_EVENTS) pane.addEventListener(type, stop, { passive: true })
  hold()
  return stop
}
