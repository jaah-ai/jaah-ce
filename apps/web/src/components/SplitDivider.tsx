// THE DRAGGABLE STRIP BETWEEN TWO PANES — left of a right-hand pane (the web console's dock,
// components/ConsoleDock.tsx; the list/detail split, components/ListDetail.tsx) or, `horizontal`, below a top
// pane (the artifacts chooser, components/tasks/TaskArtifacts.tsx). 8px of hit area with a 1px rule,
// FOCUSABLE with arrow keys (`separator` + `aria-valuenow` means "resizable split" to a screen reader).
// It owns the gesture only; what a position MEANS is the caller's `onMove`.
import { useEffect, useRef, useState } from 'react'
import Box from '@mui/material/Box'
import { DIVIDER_PX } from '../lib/splitPane'

/** 24px ≈ 3 terminal columns (or one list row) per arrow press. */
const KEY_STEP_PX = 24

/** `vertical` = a vertical strip moved along x (the default); `horizontal` = a horizontal strip moved along y. */
export type SplitOrientation = 'vertical' | 'horizontal'

export default function SplitDivider({
  valueNow,
  edge,
  onMove,
  onReset,
  ariaLabel,
  testId,
  orientation = 'vertical',
}: {
  /** The pane's share, 0–100, for `aria-valuenow`. */
  valueNow: number
  /** The strip's current left (`horizontal`: top) edge in px — where a keyboard step starts from. */
  edge: () => number
  /** A pointer (or keyboard) position along the axis — `clientX` (`horizontal`: `clientY`) — and where in
   *  the strip the pointer went down. */
  onMove: (position: number, grabOffset: number) => void
  /** Double-click, Home or Enter: back to the default split. */
  onReset: () => void
  ariaLabel: string
  testId: string
  orientation?: SplitOrientation
}) {
  const horizontal = orientation === 'horizontal'
  const [dragging, setDragging] = useState(false)
  // Window listeners, not `setPointerCapture` (unimplemented in jsdom), and they keep tracking when the
  // pointer leaves the 8px strip.
  const teardownRef = useRef<(() => void) | null>(null)
  useEffect(() => () => teardownRef.current?.(), [])

  const startDrag = (e: React.PointerEvent) => {
    // A drag already owns the listeners; overwriting `teardownRef` would orphan them (second pointer, a
    // swallowed `pointerup`).
    if (e.button !== 0 || teardownRef.current) return
    e.preventDefault()
    setDragging(true)
    // Grab offset, so the strip stays under the finger.
    const rect = e.currentTarget.getBoundingClientRect()
    const grabOffset = horizontal ? e.clientY - rect.top : e.clientX - rect.left
    const onPointerMove = (ev: PointerEvent) => onMove(horizontal ? ev.clientY : ev.clientX, grabOffset)
    const onPointerUp = () => {
      window.removeEventListener('pointermove', onPointerMove)
      window.removeEventListener('pointerup', onPointerUp)
      teardownRef.current = null
      setDragging(false)
    }
    window.addEventListener('pointermove', onPointerMove)
    window.addEventListener('pointerup', onPointerUp)
    teardownRef.current = onPointerUp
  }

  const onKeyDown = (e: React.KeyboardEvent) => {
    // The arrow moves the strip; what that does to a pane is the caller's arithmetic.
    const [back, forward] = horizontal ? ['ArrowUp', 'ArrowDown'] : ['ArrowLeft', 'ArrowRight']
    if (e.key === back) onMove(edge() - KEY_STEP_PX, 0)
    else if (e.key === forward) onMove(edge() + KEY_STEP_PX, 0)
    else if (e.key === 'Home' || e.key === 'Enter') onReset()
    else return
    e.preventDefault()
  }

  return (
    <Box
      data-testid={testId}
      role="separator"
      aria-orientation={orientation}
      aria-label={ariaLabel}
      /* The only affordance for the double-click reset. */
      title="Double-click to reset"
      aria-valuenow={Math.round(valueNow)}
      aria-valuemin={0}
      aria-valuemax={100}
      tabIndex={0}
      onPointerDown={startDrag}
      onDoubleClick={onReset}
      onKeyDown={onKeyDown}
      sx={{
        flex: `0 0 ${DIVIDER_PX}px`,
        alignSelf: 'stretch',
        cursor: horizontal ? 'row-resize' : 'col-resize',
        position: 'relative',
        // Stops a drag from selecting the page.
        userSelect: 'none',
        touchAction: 'none',
        bgcolor: dragging ? 'primary.main' : 'transparent',
        transition: 'background-color 120ms ease',
        '&:hover': { bgcolor: 'primary.main' },
        '&:focus-visible': { outline: 'none', bgcolor: 'primary.main' },
        // The 1px rule as a child, so the 8px hit area stays invisible.
        '&::after': {
          content: '""',
          position: 'absolute',
          bgcolor: 'divider',
          ...(horizontal
            ? { left: 0, right: 0, top: '50%', height: '1px', transform: 'translateY(-50%)' }
            : { top: 0, bottom: 0, left: '50%', width: '1px', transform: 'translateX(-50%)' }),
        },
      }}
    />
  )
}
