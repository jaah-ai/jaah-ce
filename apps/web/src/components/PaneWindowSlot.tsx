import { createContext, useCallback, useContext, useRef, useState, type JSX } from 'react'
import Box from '@mui/material/Box'

/* WHERE THE SPLIT PANE'S WINDOW BUTTONS SIT (Maximize/Restore, Close; `components/ListDetail.tsx`): on the page's own
 * header row, at its far end, rather than on a row of their own above it. Inside the pane, `ui/PageHeader` draws an
 * empty slot and registers it here; the pane portals its buttons into the FIRST registered slot in document order (the
 * page's own header, never a section header further down). No slot (a page without a header row, a loading state):
 * the pane keeps its own row. `null` outside the pane: a full page draws no slot. */
type Register = (node: HTMLElement, present: boolean) => void

const PaneWindowSlotContext = createContext<Register | null>(null)

export const PaneWindowSlotProvider = PaneWindowSlotContext.Provider

/** The pane's side: the registry its headers report to, and the slot its buttons go into (`null` = none). */
export function usePaneWindowSlots(): { register: Register; slot: HTMLElement | null } {
  const [slots, setSlots] = useState<readonly HTMLElement[]>([])
  const register = useCallback<Register>((node, present) => {
    setSlots(prev => {
      if (present === prev.includes(node)) return prev
      return present ? [...prev, node] : prev.filter(n => n !== node)
    })
  }, [])
  const slot = slots.reduce<HTMLElement | null>(
    (first, node) => (first === null || first.compareDocumentPosition(node) & Node.DOCUMENT_POSITION_PRECEDING ? node : first),
    null,
  )
  return { register, slot }
}

/** The header's side: an empty box at the row's far end, registered while mounted. Nothing outside the pane. */
export function PaneWindowSlot(): JSX.Element | null {
  const register = useContext(PaneWindowSlotContext)
  const current = useRef<HTMLElement | null>(null)
  const ref = useCallback((node: HTMLElement | null) => {
    if (!register) return
    if (current.current) register(current.current, false)
    current.current = node
    // A dialog's header sees the pane's context through its portal but is not on the pane's page.
    if (node && !node.closest('[role="dialog"]')) register(node, true)
  }, [register])
  if (!register) return null
  return (
    <Box
      ref={ref}
      data-slot="pane-window"
      sx={{
        flex: '0 0 auto',
        ml: 'auto',
        display: 'inline-flex',
        alignItems: 'center',
        gap: 0.5,
        // A hairline between the page's own actions and the window's; none beside a bare title.
        '[data-slot="actions"] + &': { borderLeft: 1, borderColor: 'divider', pl: 0.5 },
        '&:empty': { display: 'none' },
      }}
    />
  )
}
