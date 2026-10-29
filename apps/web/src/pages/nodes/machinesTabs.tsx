import type { ReactNode } from 'react'
import Box from '@mui/material/Box'
import { useAuth, useCan } from '../../auth'
import { Tabs } from '../../components/Tabs'
import { isEnabled } from '../../lib/editionFeatures'
import { PHONE, PHONE_TAB_STRIP_SX } from '../../ui/listStyles'

/* ── DIRECTORY > MACHINES' SUB-TABS: the single table, gates and strip ─────────────────────────────────
   NODES — every node, one flat table — HOSTS — every machine (`GET /api/hosts`), one flat table — and POOLS
   (node groups: standing instructions that create and destroy nodes), all panels of `pages/Nodes.tsx` at `/nodes`,
   the tab in `?tab=` (hooks/useUrlTab.ts). Connections and NodeLink live under Networks (`networksTabs.ts`), the
   runner accounts under Agents (`pages/agents/agentsTabs.tsx`).
   Gates: Nodes is `nodes:view`, the route's guard; Hosts and Pools are the operator's alone, as are their detail
   routes (`routesCloud.tsx`). Presentation only (`components/RequireFeature.tsx`). */
export type MachinesTab = 'nodes' | 'hosts' | 'groups'

const MACHINES_TABS: readonly { id: MachinesTab; label: string }[] = [
  { id: 'nodes', label: 'Nodes' },
  { id: 'hosts', label: 'Hosts' },
  { id: 'groups', label: 'Pools' },
]

/** Which sub-tabs this viewer is offered: the strip's tabs, in order, and a lookup by id. */
export function useMachinesTabs() {
  const { admin } = useAuth()
  const can = useCan()
  const nodes = isEnabled('cloud_nodes') && can('nodes:view')
  const operator = nodes && admin?.is_operator === true
  const shown: Record<MachinesTab, boolean> = { nodes, hosts: operator, groups: operator }
  return { tabs: MACHINES_TABS.filter(t => shown[t.id]), shown }
}

/** The Machines strip, drawn by `Nodes.tsx`, whose `?tab=` a pick sets. */
export function MachinesStrip({ tab, onSelect, actions }: {
  tab: MachinesTab
  onSelect: (next: MachinesTab) => void
  actions?: ReactNode
}) {
  const { tabs } = useMachinesTabs()
  return (
    /* The phone density rides the WRAPPER (`components/Tabs` takes no `sx`), as in `NodeDetailPage`. */
    /* Unshrinkable tabs, so the fit's width probes measure whole labels (`RouteTabLayout.tsx`). */
    <Box sx={[PHONE_TAB_STRIP_SX, { [PHONE]: { '& .MuiTab-root': { flexShrink: 0 } } }]}>
      <Tabs
        tabs={tabs}
        tab={tab}
        onSelect={onSelect}
        label="Node sections"
        idPrefix="nodes"
        /* One line at every width, the tabs that do not fit behind More (`components/Tabs`), as the Directory
           strip (`components/RouteTabLayout.tsx`): scrolled, the list pane beside an open editor showed only the
           active tab behind a chevron. */
        overflowMenu
        /* The shown tab's verb (Add node). The selected tab names the page, so no title row. On the
           strip's row at every width: a phone's verb is a wordless ＋ (`ui/CompactVerb`). */
        compactActions
        actions={actions}
      />
    </Box>
  )
}
