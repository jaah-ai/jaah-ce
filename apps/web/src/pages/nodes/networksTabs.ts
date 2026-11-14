import type { Admin } from '../../auth'
import { type Can, type TabGate, tabsShown, useShownTabs } from '../../lib/gatedTabs'

/* ── DIRECTORY > NETWORKS' SUB-TABS: the single table, gates and strip ─────────────────────────────────
   CONNECTIONS (where a cloud node launches, `settings/cloudConnections`) and NODELINK (the org's NodeLinks,
   `nodes/nodeLinkStore`), panels of `NetworksPage.tsx` at `/networks`, the tab in `?tab=` (hooks/useUrlTab.ts).
   Gates (`lib/gatedTabs.ts`), each the twin of its route or API: both need `nodes:view` (the route's guard,
   `routesCloud.tsx`); Connections is org-admin only, its route being `@org_admin`, and `system:view`; NodeLink is
   `credentials:view` and, for a tenant, the plan's NodeLink switch. The Directory tab (`lib/directoryTabs.ts`) is
   offered when either is. */
export type NetworksTab = 'connections' | 'nodelink'

const NETWORKS_TABS: readonly ({ id: NetworksTab; label: string } & TabGate)[] = [
  { id: 'connections', label: 'Connections', feature: 'cloud_nodes', orgAdminOnly: true, requires: ['nodes:view', 'system:view'] },
  { id: 'nodelink', label: 'NodeLink', feature: 'cloud_nodes', requires: ['nodes:view', 'credentials:view'], planSurface: 'nodelink_enabled' },
]

/** Which sub-tabs a viewer is offered — pure, so the Directory strip asks the same question. */
export function networksShown(admin: Admin | null | undefined, can: Can): Record<NetworksTab, boolean> {
  return tabsShown(NETWORKS_TABS, admin, can)
}

/** The strip's tabs this viewer is offered, in order. */
export function useNetworksTabs() {
  return useShownTabs(NETWORKS_TABS)
}
