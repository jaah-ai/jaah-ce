import type { JSX, MouseEvent, ReactNode, SyntheticEvent } from 'react'
import { useEffect, useRef, useState } from 'react'
import Box from '@mui/material/Box'
import Tooltip from '@mui/material/Tooltip'

/* A disabled control's reason, on hover AND tap. A disabled MUI button takes no pointer events, so its own `title`
   never shows; the reason rides a wrapping span instead. With no `reason` it lays out as the bare child. Pair it with
   `disabled` (and the same words in the control's own `title`, for assistive tech) at the call site.
   The wrapper stays mounted when the reason clears (`display: contents`, so it lays out as nothing): a control
   that becomes enabled is the same element, not a remount that would drop its focus.
   The tooltip owns the span's `title` (`describeChild` keeps it there while closed), so hover and tap show one
   sentence. CONTROLLED, OPENED BY THE CLICK A TAP DELIVERS — the same mechanics as `FeedList`'s disc tip: a touch
   has no hover, and MUI's own touch path does not open on this (enabled) span. */
export function DisabledReason({ reason, children }: { reason: string | undefined; children: ReactNode }): JSX.Element {
  const [open, setOpen] = useState(false)
  const tappedRef = useRef(false)
  // No reason, no tip: a hover or focus while enabled must not leave it armed to pop open when a reason appears.
  useEffect(() => { if (!reason) { tappedRef.current = false; setOpen(false) } }, [reason])
  const toggle = (event: MouseEvent<HTMLElement>) => {
    event.stopPropagation()
    // A click on a tip the hover already opened pins it rather than closing what the reader is reading.
    const next = !open || !tappedRef.current
    tappedRef.current = next
    setOpen(next)
  }
  // A tapped tip ignores the `mouseleave` a touch browser fires as the popper lands: only the touch leave timer, Escape
  // or a second tap closes it.
  const close = (event: Event | SyntheticEvent) => {
    if (tappedRef.current && event.type !== 'touchend' && event.type !== 'keydown') return
    tappedRef.current = false
    setOpen(false)
  }
  return (
    <Tooltip
      title={reason ?? ''}
      open={Boolean(reason) && open}
      onOpen={() => { if (reason) setOpen(true) }}
      onClose={close}
      describeChild
      leaveTouchDelay={6000}
    >
      <Box
        component="span"
        data-disabled-reason={reason}
        onClick={reason ? toggle : undefined}
        sx={reason ? { display: 'inline-flex', cursor: 'not-allowed' } : { display: 'contents' }}
      >
        {children}
      </Box>
    </Tooltip>
  )
}
