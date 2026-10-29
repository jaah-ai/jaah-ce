import { createContext, type ReactNode, useContext } from 'react'

/* Where a tabbed page's controls go when its strip is drawn by a LAYOUT (`RouteTabLayout`) rather than by the
 * page: the strip's right end, an element the layout publishes here. `ui/PageHeader` reads it — a header
 * with no title portals its `actions` there, so a tab's page carries no heading row of its own (the selected
 * tab already names it). `null` outside such a layout: the header then renders in place. `undefined` while
 * the layout's strip has not mounted its slot yet: the header waits (renders nothing) rather than drawing
 * in place for one frame and jumping onto the strip. */
export const TabActionsSlot = createContext<HTMLElement | null | undefined>(null)

export function useTabActionsSlot(): HTMLElement | null | undefined {
  return useContext(TabActionsSlot)
}

/* For a page that keeps several tabs' panels mounted at once (`pages/users/UsersPage.tsx`): only the SHOWN panel may reach the strip, or a hidden panel's
 * actions would sit on it too. */
export function TabActionsGate({ shown, children }: { shown: boolean; children: ReactNode }) {
  const slot = useTabActionsSlot()
  return <TabActionsSlot.Provider value={shown ? slot : null}>{children}</TabActionsSlot.Provider>
}
