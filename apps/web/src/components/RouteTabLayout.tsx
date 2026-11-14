import { Suspense, useCallback, useState } from 'react'
import Box from '@mui/material/Box'
import { Outlet, useLocation, useNavigate } from 'react-router-dom'
import { customizeTabAt, useCustomizeTabs } from '../lib/customizeTabs'
import { directoryTabAt, useDirectoryTabs } from '../lib/directoryTabs'
import { agentsTabAt, useAgentsTabs } from '../pages/agents/agentsTabs'
import LoadingState from '../ui/LoadingState'
import { TabActionsSlot } from './TabActionsSlot'
import { TabbedPanels } from './Tabs'

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
    <TabbedPanels
      tabs={tabs}
      tab={active.id}
      onSelect={selectTab}
      label={label}
      idPrefix={name}
      strip={{ phone: true, sx: { mb: 2 }, testId: `${name}-tabs` }}
      /* One line at every width, the tabs that do not fit behind More (`components/Tabs`): wrapped,
         Directory's Agents sat alone on a second line at 360px; scrolled, the list pane beside
         an open detail pane clipped Secrets to "S" behind a chevron. */
      overflowMenu
      /* The active page's `PageHeader` actions portal here (`TabActionsSlot`), on the strip's row at every
         width: each page's verb drops its word on a phone (`ui/CompactVerb`). */
      compactActions
      actions={<Box component="span" ref={setActionsSlot} sx={{ display: 'contents' }} />}
    >
      {/* Only the active page is mounted (each tab is its own route); every other tab's `aria-controls` still
          resolves, to its empty hidden panel. */}
      {(_t, isActive) => isActive && (
        <TabActionsSlot.Provider value={actionsSlot ?? undefined}>
          {/* The tab's page is a lazy chunk: suspending here, not at the shell's boundary, keeps the strip drawn
              over a list skeleton while it loads — a cold editor URL (`/users/<id>`) left both panes a bare
              spinner for ~600ms. */}
          <Suspense fallback={<LoadingState rows={5} />}>
            <Outlet />
          </Suspense>
        </TabActionsSlot.Provider>
      )}
    </TabbedPanels>
  )
}

export function DirectoryLayout() {
  const here = directoryTabAt(useLocation().pathname)
  return <RouteTabLayout tabs={useDirectoryTabs()} here={here} name="directory" label="Directory sections" />
}

/* Customize (`/playbooks`, `/templates`, `/agents`, `/compliance/packs`): what an org shapes its agents' work with. */
export function CustomizeLayout() {
  const here = customizeTabAt(useLocation().pathname)
  return <RouteTabLayout tabs={useCustomizeTabs()} here={here} name="customize" label="Customize sections" />
}

/* Customize > Agents' sub-tabs (`/agents`, `/agents/catalog`, `/agents/registry`), nested under `CustomizeLayout` so
 * the Customize strip stays above this one. The strip renders even for a viewer offered only one sub-tab (decided,
 * not an oversight): it names the page and stays where readers look. */
export function AgentsLayout() {
  const here = agentsTabAt(useLocation().pathname)
  return <RouteTabLayout tabs={useAgentsTabs()} here={here} name="agents" label="Agent sections" />
}
