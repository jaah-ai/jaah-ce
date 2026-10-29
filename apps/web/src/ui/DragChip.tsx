/* WHAT IS BEING DRAGGED, AT THE POINTER — the chip a pointer drag (`hooks/usePointerDrag.ts`) draws while an
 * item is in flight, so the operator never has to remember which row they picked up.
 *
 * Mount it only while a drag is up. It follows the pointer by writing `transform` on its own node from a
 * window `pointermove` listener — never React state, so a drag re-renders nothing per pixel. Hidden until
 * the first move reports a position (the drag starts on a move past the threshold, so that is one frame).
 * `pointer-events: none` is load-bearing: the gesture resolves its destination from the element UNDER the
 * pointer, which must never be this chip. Portalled to `body` so no clipping ancestor cuts it off.
 */
import Box from '@mui/material/Box'
import Portal from '@mui/material/Portal'
import { useEffect, useRef } from 'react'
import type { JSX } from 'react'

/** Down-right of the cursor, clear of its hotspot and of the row under it. */
const OFFSET_PX = 14

/** Wide enough for a realistic full name; a longer one ellipsises (the whole string rides `title`). */
const MAX_WIDTH_PX = 360

export default function DragChip({ label }: { label: string }): JSX.Element {
  const ref = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    function follow(event: PointerEvent) {
      const node = ref.current
      if (!node) return
      node.style.transform = `translate(${event.clientX + OFFSET_PX}px, ${event.clientY + OFFSET_PX}px)`
      node.style.visibility = 'visible'
    }
    window.addEventListener('pointermove', follow)
    return () => window.removeEventListener('pointermove', follow)
  }, [])
  return (
    <Portal>
      <Box
        ref={ref}
        data-testid="drag-chip"
        role="status"
        aria-live="polite"
        title={label}
        sx={{
          position: 'fixed',
          top: 0,
          left: 0,
          zIndex: 'tooltip',
          visibility: 'hidden',
          pointerEvents: 'none',
          maxWidth: MAX_WIDTH_PX,
          px: 1,
          py: 0.375,
          borderRadius: 1,
          bgcolor: 'background.paper',
          border: 1,
          borderColor: 'divider',
          boxShadow: 3,
          fontSize: '0.8125rem',
          lineHeight: 1.3,
          whiteSpace: 'nowrap',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
        }}
      >
        {label}
      </Box>
    </Portal>
  )
}
