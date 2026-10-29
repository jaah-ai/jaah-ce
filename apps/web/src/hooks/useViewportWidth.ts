import { useEffect, useState } from 'react'

/** The window's width, live. A JS read because the console dock's width is arithmetic from a ratio, which CSS
 *  cannot hand to state (components/ConsoleDock.tsx). */
export function useViewportWidth(): number {
  const [width, setWidth] = useState(() => (typeof window === 'undefined' ? 0 : window.innerWidth))
  useEffect(() => {
    const onResize = () => setWidth(window.innerWidth)
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])
  return width
}

/** Whether the window satisfies `test` — re-rendering only when the ANSWER changes, not on every resize
 *  event (React bails out of an unchanged boolean). For a caller high in the tree (App.tsx's routing). */
export function useViewportIs(test: (width: number) => boolean): boolean {
  const [is, setIs] = useState(() => (typeof window === 'undefined' ? false : test(window.innerWidth)))
  useEffect(() => {
    const onResize = () => setIs(test(window.innerWidth))
    onResize()
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [test])
  return is
}
