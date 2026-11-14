import { type RefObject, useEffect, useRef } from 'react'
import { scrollingAncestor } from '../lib/scrollWithinPane'

/* THE CONSOLE'S ONE "LOAD MORE ON SCROLL" SENTINEL: an IntersectionObserver on a zero-height div after the list,
 * not a scroll handler — the scrollport is the shell's (a different element on the phone). Rooted at the rows' own
 * scrollport with a page of runway (`rootMargin`), so scrolling never stops dead at the last row and a nested
 * scroller does not clip the sentinel before the viewport's margin could see it. Armed while `enabled`; re-armed
 * each time it turns true again (a page landed), so a viewport taller than a page keeps reading until it is
 * filled. The caller keeps it false after a failure: a fresh observer reports the still-visible sentinel at
 * once, re-reading as fast as the server fails. Calls the LATEST render's `onMore`. */
export function useLoadMoreSentinel(enabled: boolean, onMore: () => unknown): RefObject<HTMLDivElement> {
  const ref = useRef<HTMLDivElement>(null)
  const more = useRef(onMore)
  more.current = onMore
  useEffect(() => {
    const node = ref.current
    if (!enabled || !node || typeof IntersectionObserver === 'undefined') return
    const observer = new IntersectionObserver(
      entries => { if (entries.some(e => e.isIntersecting)) void more.current() },
      { root: scrollingAncestor(node), rootMargin: '400px' },
    )
    observer.observe(node)
    return () => observer.disconnect()
  }, [enabled])
  return ref
}
