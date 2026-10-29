import { type Admin, useAuth, useCan } from '../auth'
import { networksShown } from '../pages/nodes/networksTabs'
import { AGENTS_TAB_PATHS, DIRECTORY_TAB_PATHS, MACHINES_TAB_PATHS, NETWORKS_TAB_PATHS, USERS_TAB_PATHS, normalizePathname } from '../routeGroups'
import type { PermissionKey } from '../types'
import { isEnabled } from './editionFeatures'
import { type PlanSwitch, planSwitchOn } from './planSwitches'

/* DIRECTORY — the single table of its tabs: label, addresses and the gate each address needs. Read by the tab
 * strip (`components/RouteTabLayout.tsx`) and by the rail's Directory row (`components/Sidebar.tsx`), which
 * is offered when ANY tab is and links to the first one offered.
 *
 * Every gate is the twin of that page's route guard in `App.tsx`; they move together, or the strip offers a
 * tab the route refuses. Presentation only, never the security boundary (`components/RequireFeature.tsx`). */

export type DirectoryTabId = keyof typeof DIRECTORY_TAB_PATHS

/* One address a tab answers on, the key that address's route guard asks, and the plan switch it needs on
   (`lib/planSwitches.ts`). */
type DirectoryPage = { path: string; requires?: PermissionKey; planSwitch?: PlanSwitch }

export type DirectoryTab = {
  id: DirectoryTabId
  label: string
  /* The tab's addresses: its default first, then any sub-tab with an address of its own (People's Groups,
     Agents' Custom agents). The tab is offered when ANY is readable and links to the first readable one. */
  pages: readonly DirectoryPage[]
  /* Offered only while this holds as well — a tab whose sub-tabs share one address but not one gate (Networks). */
  shown?: (admin: Admin | null | undefined, can: (key: PermissionKey) => boolean) => boolean
}

/** A tab as one viewer is offered it: `path` is the first of its pages that viewer may read. */
export type OfferedDirectoryTab = DirectoryTab & { path: string }

const ALL_DIRECTORY_TABS: readonly DirectoryTab[] = [
  { id: 'projects', label: 'Projects', pages: [{ path: DIRECTORY_TAB_PATHS.projects, requires: 'projects:view' }] },
  /* "Machines": nodes, and the operator's hosts and pools (`pages/Nodes.tsx`, `pages/nodes/machinesTabs.tsx`). */
  { id: 'nodes', label: 'Machines', pages: [{ path: MACHINES_TAB_PATHS.nodes, requires: 'nodes:view' }] },
  /* The runner account roster (`/accounts`, `routes/accounts.py`) whatever the plan, then SDK agents, the
     connector catalog and the ACP registry (`pages/agents/`), all `agents:view` (`routes/agents.py`), and only while
     the provider grid has Agents on (`sdk` or an `acp:` row). Every sub-tab is listed, or its page loses the strip. */
  {
    id: 'agents',
    label: 'Agents',
    pages: [
      { path: AGENTS_TAB_PATHS.accounts, requires: 'accounts:view' },
      { path: AGENTS_TAB_PATHS.agents, requires: 'agents:view', planSwitch: 'agents' },
      { path: AGENTS_TAB_PATHS.catalog, requires: 'agents:view', planSwitch: 'agents' },
      { path: AGENTS_TAB_PATHS.registry, requires: 'agents:view', planSwitch: 'agents' },
    ],
  },
  /* No key: every signed-in user reads it — the API scopes a keyless session to the rows it
   * created (`authz.owned_credential_clause`). Labelled Secrets; the code and address stay `credentials`. */
  { id: 'credentials', label: 'Secrets', pages: [{ path: DIRECTORY_TAB_PATHS.credentials }] },
  /* People with Groups as its sub-tab (`pages/users/UsersPage.tsx`). */
  {
    id: 'people',
    label: 'People',
    pages: [
      { path: USERS_TAB_PATHS.users, requires: 'users:view' },
      { path: USERS_TAB_PATHS.groups, requires: 'groups:view' },
      { path: USERS_TAB_PATHS.access, requires: 'users:view' },
    ],
  },
  /* Cloud connections and NodeLink (`pages/nodes/NetworksPage.tsx`), one address under the route's `nodes:view`,
     offered when either sub-tab is (`pages/nodes/networksTabs.ts`). */
  {
    id: 'networks',
    label: 'Networks',
    pages: [{ path: NETWORKS_TAB_PATHS.networks, requires: 'nodes:view' }],
    shown: (admin, can) => Object.values(networksShown(admin, can)).some(Boolean),
  },
]

/* Without Users and invitations there is no People; without Cloud nodes there are no Machines or Networks. */
export const DIRECTORY_TABS: readonly DirectoryTab[] = ALL_DIRECTORY_TABS
  .filter(tab => tab.id !== 'people' || isEnabled('users_invites'))
  .filter(tab => (tab.id !== 'nodes' && tab.id !== 'networks') || isEnabled('cloud_nodes'))

/** The tabs this viewer is offered, in strip order. */
export function useDirectoryTabs(): readonly OfferedDirectoryTab[] {
  const { admin } = useAuth()
  const can = useCan()
  return DIRECTORY_TABS.flatMap(tab => {
    if (tab.shown && !tab.shown(admin, can)) return []
    const page = tab.pages.find(p => (!p.requires || can(p.requires)) && (!p.planSwitch || planSwitchOn(admin, p.planSwitch)))
    return page ? [{ ...tab, path: page.path }] : []
  })
}

/** The tab whose LIST page this pathname is — exact, so an editor (`/projects/12`) is no tab's page. */
export function directoryTabAt(pathname: string): DirectoryTab | undefined {
  const path = normalizePathname(pathname)
  return DIRECTORY_TABS.find(tab => tab.pages.some(p => p.path === path))
}

/** Whether a pathname lies anywhere in Directory — a list page or one of its editors (`/projects/12`,
 *  `/nodes/<id>`, `/accounts/new`). The rail's Directory row is active across all of them. */
export function isInDirectory(pathname: string): boolean {
  return DIRECTORY_TABS.some(tab =>
    tab.pages.some(p => pathname === p.path || pathname.startsWith(`${p.path}/`)))
}
