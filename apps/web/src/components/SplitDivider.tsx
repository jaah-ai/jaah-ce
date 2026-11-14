// THE DRAGGABLE STRIP BETWEEN TWO PANES — left of a right-hand pane (the web console's dock,
// components/ConsoleDock.tsx; the list/detail split, components/ListDetail.tsx) or, `horizontal`, below a top
// pane (the artifacts chooser, components/tasks/TaskArtifacts.tsx). 8px of hit area with a 1px rule,
// FOCUSABLE with arrow keys (`separator` + `aria-valuenow` means "resizable split" to a screen reader).
// It owns the gesture only; what a position MEANS is the caller's `onMove`.
// NO HOVER TOOLTIP: a tip on a full-height strip opens past the viewport's edge, grows the page a scrollbar,
// shifts the strip from under the pointer and closes — a loop that shook the page every ~0.5s.
import { useState } from 'react'
import Box from '@mui/material/Box'
import { DIVIDER_PX } from '../lib/splitPane'
import { useWindowDrag } from '../hooks/useWindowDrag'

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
  const attachDrag = useWindowDrag()

  const startDrag = (e: React.PointerEvent) => {
    if (e.button !== 0) return
    // Grab offset, so the strip stays under the finger.
    const rect = e.currentTarget.getBoundingClientRect()
    const grabOffset = horizontal ? e.clientY - rect.top : e.clientX - rect.left
    const onPointerMove = (ev: PointerEvent) => onMove(horizontal ? ev.clientY : ev.clientX, grabOffset)
    // Refused while a drag owns the listeners (second pointer, a swallowed `pointerup`).
    if (!attachDrag(onPointerMove, () => setDragging(false))) return
    e.preventDefault()
    setDragging(true)
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
      aria-description="Double-click or Home to reset"
      aria-valuenow={Math.round(valueNow)}
      aria-valuemin={0}
      aria-valuemax={100}
      tabIndex={0}
      onPointerDown={startDrag}
      onDoubleClick={onReset}
      onKeyDown={onKeyDown}
      sx={{
        flexGrow: 0,
        flexShrink: 0,
        flexBasis: DIVIDER_PX,
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
          borderColor: 'divider',
          ...(horizontal
            ? { left: 0, right: 0, top: '50%', height: 0, borderTop: 1, transform: 'translateY(-50%)' }
            : { top: 0, bottom: 0, left: '50%', width: 0, borderLeft: 1, transform: 'translateX(-50%)' }),
        },
      }}
    />
  )
}
