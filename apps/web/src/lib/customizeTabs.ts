import { AGENTS_TAB_PATHS, CUSTOMIZE_TAB_PATHS } from '../routeGroups'
import { type RouteTab, editionTabs, isUnderTabs, routeTabAt, useOfferedTabs } from './gatedTabs'

/* CUSTOMIZE — the single table of its tabs: label, addresses and the gate each address needs (`lib/gatedTabs.ts`). Read by
 * the tab strip (`components/RouteTabLayout.tsx`) and the rail's Customize row (`components/Sidebar.tsx`), which is
 * offered when ANY tab is and links to the first one offered.
 *
 * Every gate is the twin of that page's route guard in `App.tsx` (Compliance Packs': `routesCloud.tsx`); they move
 * together, or the strip offers a tab the route refuses. Presentation only, never the security boundary
 * (`components/RequireFeature.tsx`). */

export type CustomizeTabId = keyof typeof CUSTOMIZE_TAB_PATHS

export type CustomizeTab = RouteTab<CustomizeTabId>

const ALL_CUSTOMIZE_TABS: readonly CustomizeTab[] = [
  /* Playbooks (`pages/playbooks/`): every org member reads while the plan has Playbooks on; always the operator. */
  { id: 'playbooks', label: 'Playbooks', pages: [{ path: CUSTOMIZE_TAB_PATHS.playbooks, planSurface: 'playbooks_enabled' }] },
  /* The template gallery (`pages/templates/TemplatesPage.tsx`), every member's; its manage and editor pages sit under it. */
  { id: 'templates', label: 'Templates', pages: [{ path: CUSTOMIZE_TAB_PATHS.templates }] },
  /* The org's SDK agents, the agent marketplace and the ACP registry (`pages/agents/`), all `agents:view`
     (`routes/agents.py`); all but the marketplace (Catalog) only while the provider grid has Agents on. */
  {
    id: 'agents',
    label: 'Agents',
    pages: [
      { path: AGENTS_TAB_PATHS.agents, requires: 'agents:view', planSwitch: 'agents' },
      { path: AGENTS_TAB_PATHS.catalog, requires: 'agents:view' },
      { path: AGENTS_TAB_PATHS.registry, requires: 'agents:view', planSwitch: ['agents', 'acp_registry'] },
    ],
  },
  /* Portfolio Compliance's packs (`pages/compliance/PacksTab.tsx`): `compliance:view` while the org's plan has
     Compliance on (`RequireCompliance`); a pack's rule pages sit under it. */
  {
    id: 'compliancePacks',
    label: 'Compliance Packs',
    feature: 'compliance',
    pages: [{ path: CUSTOMIZE_TAB_PATHS.compliancePacks, requires: 'compliance:view', shown: admin => admin?.compliance_enabled === true }],
    under: ['/compliance/rules'],
  },
]

export const CUSTOMIZE_TABS = editionTabs(ALL_CUSTOMIZE_TABS)

export const useCustomizeTabs = () => useOfferedTabs(CUSTOMIZE_TABS)
export const customizeTabAt = (pathname: string) => routeTabAt(CUSTOMIZE_TABS, pathname)
export const isInCustomize = (pathname: string) => isUnderTabs(CUSTOMIZE_TABS, pathname)
