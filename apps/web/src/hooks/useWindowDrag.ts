/* One pointer gesture's window listeners, shared by SplitDivider, ConsoleTerminal and LayoutDesigner: window
 * `pointermove`/`pointerup` (jsdom lacks pointer capture; tracking survives the pointer leaving the handle), torn
 * down on `pointerup` and unmount, `onUp` after the teardown. A gesture that owns the listeners is never
 * overwritten: a second pointerdown is refused. What a position means (offset, clamps, state) stays with the caller. */
import { useCallback, useEffect, useRef } from 'react'

/** Returns `attach(onMove, onUp?)`: `true` when it took the listeners, `false` while a gesture owns them. */
export function useWindowDrag(): (onMove: (ev: PointerEvent) => void, onUp?: (ev: PointerEvent) => void) => boolean {
  const teardownRef = useRef<(() => void) | null>(null)
  useEffect(() => () => teardownRef.current?.(), [])

  return useCallback((onMove, onUp) => {
    if (teardownRef.current) return false
    const teardown = () => {
      window.removeEventListener('pointermove', onMove)
      window.removeEventListener('pointerup', onPointerUp)
      teardownRef.current = null
    }
    const onPointerUp = (ev: PointerEvent) => {
      teardown()
      onUp?.(ev)
    }
    window.addEventListener('pointermove', onMove)
    window.addEventListener('pointerup', onPointerUp)
    teardownRef.current = teardown
    return true
  }, [])
}
