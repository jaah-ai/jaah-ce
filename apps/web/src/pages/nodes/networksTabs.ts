import { type Admin, useAuth, useCan } from '../../auth'
import { isEnabled } from '../../lib/editionFeatures'
import { isOrgAdminSeat } from '../../lib/orgAdmin'
import { planSurfaceShown } from '../../lib/planSwitches'
import type { PermissionKey } from '../../types'

/* ── DIRECTORY > NETWORKS' SUB-TABS: the single table, gates and strip ─────────────────────────────────
   CONNECTIONS (where a cloud node launches, `settings/cloudConnections`) and NODELINK (the org's NodeLinks,
   `nodes/nodeLinkStore`), panels of `NetworksPage.tsx` at `/networks`, the tab in `?tab=` (hooks/useUrlTab.ts).
   Gates, each the twin of its route or API: both need `nodes:view` (the route's guard, `routesCloud.tsx`);
   Connections is org-admin only, its route being `@org_admin`, and `system:view`; NodeLink is `credentials:view`
   and, for a tenant, the plan's NodeLink switch (`lib/planSwitches.ts::planSurfaceShown`). The Directory tab
   (`lib/directoryTabs.ts`) is offered when either is. Presentation only (`components/RequireFeature.tsx`). */
export type NetworksTab = 'connections' | 'nodelink'

const NETWORKS_TABS: readonly { id: NetworksTab; label: string }[] = [
  { id: 'connections', label: 'Connections' },
  { id: 'nodelink', label: 'NodeLink' },
]

/** Which sub-tabs a viewer is offered — pure, so the Directory strip asks the same question. */
export function networksShown(admin: Admin | null | undefined, can: (key: PermissionKey) => boolean): Record<NetworksTab, boolean> {
  const nodes = isEnabled('cloud_nodes') && can('nodes:view')
  return {
    connections: nodes && isOrgAdminSeat(admin) && can('system:view'),
    nodelink: nodes && can('credentials:view') && planSurfaceShown(admin, 'nodelink_enabled'),
  }
}

/** The strip's tabs this viewer is offered, in order. */
export function useNetworksTabs() {
  const { admin } = useAuth()
  const shown = networksShown(admin, useCan())
  return NETWORKS_TABS.filter(t => shown[t.id])
}
