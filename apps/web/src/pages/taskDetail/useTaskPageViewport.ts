// The Task page's software-keyboard guard: publishes `--task-page-vv-height` and `data-keyboard` for `PAGE_SX`.
import { useEffect, useRef } from 'react'

/** The page element's ref, to hand `DetailPage`. The guard re-runs once that element exists. */
export function useTaskPageViewport(
  { hasTask, isCreate, loading, settled }: { hasTask: boolean; isCreate: boolean; loading: boolean; settled: boolean },
) {
  const pageRef = useRef<HTMLDivElement | null>(null)
  // AC1d.2 — the software-keyboard guard. `100dvh` ignores the on-screen keyboard; `visualViewport`
  // tracks it, so this publishes the height available BELOW this element's top for `PAGE_SX`'s
  // `minHeight`. Load-bearing details: ignore a transient `0`; ONE write per animation frame (Android
  // Chrome fires continuously); NO `offsetTop` compensation (the shell's `overflow: hidden` pins it at 0).
  useEffect(() => {
    const vv = window.visualViewport
    const page = pageRef.current
    if (!vv || !page) return
    let frame: number | null = null
    const apply = () => {
      if (!(vv.height > 0)) return
      if (frame !== null) return
      frame = requestAnimationFrame(() => {
        frame = null
        if (!(vv.height > 0)) return
        const top = page.getBoundingClientRect().top
        page.style.setProperty('--task-page-vv-height', `${Math.max(0, vv.height - top)}px`)
        // …and whether the keyboard is up: the height is published regardless, and the form's ceiling
        // must not apply at rest. Visual viewport >120px shorter than layout = keyboard (~260-330px on a
        // phone; URL-bar and pinch-zoom deltas are a few px).
        page.dataset.keyboard = window.innerHeight - vv.height > 120 ? 'up' : 'down'
      })
    }
    apply()
    vv.addEventListener('resize', apply)
    vv.addEventListener('scroll', apply)
    return () => {
      if (frame !== null) cancelAnimationFrame(frame)
      vv.removeEventListener('resize', apply)
      vv.removeEventListener('scroll', apply)
      page.style.removeProperty('--task-page-vv-height')
      delete page.dataset.keyboard
    }
    // Re-runs once the page element exists (null through loading/not-found). `loading` is a dep because
    // create mode renders the loading shell until the roster lands and has no task to key on. `settled` (`runMetrics.settled`):
    // the shell also waits for the run-metrics read, which starts only once the task has landed.
  }, [hasTask, isCreate, loading, settled])
  return pageRef
}
