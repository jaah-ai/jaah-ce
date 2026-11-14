import { networksShown } from '../pages/nodes/networksTabs'
import { DIRECTORY_TAB_PATHS, MACHINES_TAB_PATHS, NETWORKS_TAB_PATHS, USERS_TAB_PATHS } from '../routeGroups'
import { type RouteTab, editionTabs, isUnderTabs, routeTabAt, useOfferedTabs } from './gatedTabs'

/* DIRECTORY — the single table of its tabs: label, addresses and the gate each address needs (`lib/gatedTabs.ts`). Read by the tab
 * strip (`components/RouteTabLayout.tsx`) and by the rail's Directory row (`components/Sidebar.tsx`), which
 * is offered when ANY tab is and links to the first one offered.
 *
 * Every gate is the twin of that page's route guard in `App.tsx`; they move together, or the strip offers a
 * tab the route refuses. Presentation only, never the security boundary (`components/RequireFeature.tsx`). */

export type DirectoryTabId = keyof typeof DIRECTORY_TAB_PATHS

/* A tab's own gate: its edition feature, and `shown` for a tab whose sub-tabs share one address but not one gate
   (Networks). */
export type DirectoryTab = RouteTab<DirectoryTabId>

const ALL_DIRECTORY_TABS: readonly DirectoryTab[] = [
  { id: 'projects', label: 'Projects', pages: [{ path: DIRECTORY_TAB_PATHS.projects, requires: 'projects:view' }] },
  /* "Machines": nodes, and the operator's hosts and pools (`pages/Nodes.tsx`, `pages/nodes/machinesTabs.tsx`). */
  { id: 'nodes', label: 'Machines', feature: 'cloud_nodes', pages: [{ path: MACHINES_TAB_PATHS.nodes, requires: 'nodes:view' }] },
  /* AI Accounts: the runner account roster (`/accounts`, `routes/accounts.py`) whatever the plan. The org's SDK agents,
     catalog and registry are Customize > Agents (`lib/customizeTabs.ts`). */
  { id: 'accounts', label: 'AI Accounts', pages: [{ path: DIRECTORY_TAB_PATHS.accounts, requires: 'accounts:view' }] },
  /* No key: every signed-in user reads it — the API scopes a keyless session to the rows it
   * created (`authz.owned_credential_clause`). Labelled Secrets; the code and address stay `credentials`. */
  { id: 'credentials', label: 'Secrets', pages: [{ path: DIRECTORY_TAB_PATHS.credentials }] },
  /* People with Groups as its sub-tab (`pages/users/UsersPage.tsx`). */
  {
    id: 'people',
    label: 'People',
    feature: 'users_invites',
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
    feature: 'cloud_nodes',
    pages: [{ path: NETWORKS_TAB_PATHS.networks, requires: 'nodes:view' }],
    shown: (admin, can) => Object.values(networksShown(admin, can)).some(Boolean),
  },
]

/* Without Users and invitations there is no People; without Cloud nodes there are no Machines or Networks. */
export const DIRECTORY_TABS = editionTabs(ALL_DIRECTORY_TABS)

export const useDirectoryTabs = () => useOfferedTabs(DIRECTORY_TABS)
export const directoryTabAt = (pathname: string) => routeTabAt(DIRECTORY_TABS, pathname)
export const isInDirectory = (pathname: string) => isUnderTabs(DIRECTORY_TABS, pathname)
