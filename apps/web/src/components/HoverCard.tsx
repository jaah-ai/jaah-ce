import { useState, useRef, useEffect, type ReactNode } from 'react'
import Popper from '@mui/material/Popper'

// A hover popover anchored to an inline trigger. The popper's `fixed` strategy escapes the table's
// overflow-x clipping, with popper.js flip/shift collision handling. Non-interactive
// (`pointer-events: none`), so it cannot steal a click.
//
// Touch has no hover, so a tap PINS the card open (mouse leave will not dismiss it); tapping the trigger
// again, or outside it, dismisses it.
//
// `data-testid="hovercard-trigger"`/`"hovercard-pop"` are the hooks every consuming suite uses.
// `disablePortal` keeps the card in the trigger's subtree, so a `container.querySelector` finds both.
export default function HoverCard({ children, content }: { children: ReactNode; content: ReactNode }) {
  const [open, setOpen] = useState(false)
  const [pinned, setPinned] = useState(false)
  const ref = useRef<HTMLSpanElement>(null)

  // While pinned, a tap outside or Escape dismisses it (as the ⋯ menu,
  // `components/tasks/rowActions.tsx`'s `RowActionsOverflow`).
  useEffect(() => {
    if (!pinned) return
    const dismiss = () => {
      setPinned(false)
      setOpen(false)
    }
    const onOutside = (e: PointerEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) dismiss()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') dismiss()
    }
    document.addEventListener('pointerdown', onOutside)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onOutside)
      document.removeEventListener('keydown', onKey)
    }
  }, [pinned])

  return (
    <span
      ref={ref}
      data-testid="hovercard-trigger"
      onMouseEnter={() => {
        if (!pinned) setOpen(true)
      }}
      onMouseLeave={() => {
        if (!pinned) setOpen(false)
      }}
      onClick={() => {
        if (pinned) {
          setPinned(false)
          setOpen(false)
        } else {
          setPinned(true)
          setOpen(true)
        }
      }}
    >
      {children}
      {open && (
        <Popper
          open
          anchorEl={ref.current}
          // Keeps the card inside the trigger's subtree (see above).
          disablePortal
          placement="bottom-start"
          // `fixed`, not `absolute`: `.table-wrap`'s `overflow-x` would clip the card.
          popperOptions={{ strategy: 'fixed' }}
          modifiers={[
            { name: 'offset', options: { offset: [0, 4] } },
            { name: 'preventOverflow', options: { padding: 8 } },
          ]}
          data-testid="hovercard-pop"
          sx={{ zIndex: 50 }}
        >
          {content}
        </Popper>
      )}
    </span>
  )
}
