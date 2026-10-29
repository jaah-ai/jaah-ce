import { Suspense, useCallback, useState } from 'react'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import Box from '@mui/material/Box'
import { automationTabAt, useAutomationTabs } from '../lib/automationTabs'
import { directoryTabAt, useDirectoryTabs } from '../lib/directoryTabs'
import { PHONE, PHONE_TAB_STRIP_SX } from '../ui/listStyles'
import LoadingState from '../ui/LoadingState'
import { TabActionsSlot } from './TabActionsSlot'
import { TabPanel, Tabs } from './Tabs'

type RouteTab<Id extends string> = { id: Id; label: string; path: string }

/* One tab strip over a group of list pages (`App.tsx`'s layout routes). Each tab is its own page at its own
 * address; its `PageHeader` carries no title (the tab names it) and its actions portal onto this strip's
 * right end (`TabActionsSlot`). This draws only the strip and the active tab's panel. Each group's
 * `lib/<name>Tabs.ts` is the single table of its tabs, order and gates. */
function RouteTabLayout<Id extends string>({ tabs, here, name, label }: {
  tabs: readonly RouteTab<Id>[]
  /* The tab the address belongs to (a tab with sub-tabs answers on several addresses). */
  here: { id: Id } | undefined
  name: string
  label: string
}) {
  const navigate = useNavigate()
  const [actionsSlot, setActionsSlot] = useState<HTMLElement | null>(null)
  const active = here ? tabs.find(t => t.id === here.id) : undefined

  /* `replace`, not push: the strip selects on focus, so arrow-keying would bury Back under history. */
  const selectTab = useCallback((next: Id) => {
    const target = tabs.find(t => t.id === next)
    if (target) navigate(target.path, { replace: true })
  }, [navigate, tabs])

  /* A path that is no offered tab (unreachable under the guards) renders the page plain. */
  if (!active) return <Outlet />

  /* The strip renders even over a lone tab: it names the page, and the section stays where readers look for it. */
  return (
    <>
      {/* Both route-tab strips (Directory, Automation): the phone density and unshrinkable tabs ride the wrapper
         (`components/Tabs` takes no `sx`), as on the Machines strip (`pages/nodes/machinesTabs.tsx`). Unshrinkable
         because the density's `flex: 1 1 auto` lets a tab shrink: the fit's width probes then measured padding
         only, every tab "fit", and the labels ran together. */}
      <Box data-testid={`${name}-tabs`} sx={[{ mb: 2 }, PHONE_TAB_STRIP_SX, { [PHONE]: { '& .MuiTab-root': { flexShrink: 0 } } }]}>
        <Tabs
          tabs={tabs}
          tab={active.id}
          onSelect={selectTab}
          label={label}
          idPrefix={name}
          /* One line at every width, the tabs that do not fit behind More (`components/Tabs`): wrapped,
             Directory's Agents sat alone on a second line at 360px; scrolled, the list pane beside
             an open detail pane clipped Secrets to "S" behind a chevron. */
          overflowMenu
          /* The active page's `PageHeader` actions portal here (`TabActionsSlot`), on the strip's row at every
             width: each page's verb drops its word on a phone (`ui/CompactVerb`). */
          compactActions
          actions={<span ref={setActionsSlot} style={{ display: 'contents' }} />}
        />
      </Box>
      <TabPanel id={active.id} current={active.id} idPrefix={name}>
        <TabActionsSlot.Provider value={actionsSlot ?? undefined}>
          {/* The tab's page is a lazy chunk: suspending here, not at the shell's boundary, keeps the strip drawn
              over a list skeleton while it loads — a cold editor URL (`/users/<id>`) left both panes a bare
              spinner for ~600ms. */}
          <Suspense fallback={<LoadingState rows={5} />}>
            <Outlet />
          </Suspense>
        </TabActionsSlot.Provider>
      </TabPanel>
      {/* Every other tab's `aria-controls` must resolve too: an empty hidden panel each (only the active
          page is mounted — each tab is its own route). */}
      {tabs.filter(t => t.id !== active.id).map(t => (
        <TabPanel key={t.id} id={t.id} current={active.id} idPrefix={name}>{null}</TabPanel>
      ))}
    </>
  )
}

export function DirectoryLayout() {
  const here = directoryTabAt(useLocation().pathname)
  return <RouteTabLayout tabs={useDirectoryTabs()} here={here} name="directory" label="Directory sections" />
}

/* Automations (`/automations`), Playbooks (`/playbooks`) and Slots (`/worktrees`). */
export function AutomationLayout() {
  const here = automationTabAt(useLocation().pathname)
  return <RouteTabLayout tabs={useAutomationTabs()} here={here} name="automation" label="Automation sections" />
}
