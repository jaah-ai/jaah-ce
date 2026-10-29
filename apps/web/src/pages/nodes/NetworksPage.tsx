import Box from '@mui/material/Box'
import { TabPanel, Tabs } from '../../components/Tabs'
import { useUrlTab } from '../../hooks/useUrlTab'
import { EmptyState } from '../../ui/EmptyState'
import { NODES_LIST_CONTAINER_SX, PHONE_TAB_STRIP_SX } from '../../ui/listStyles'
import CloudConnectionsSection from '../settings/cloudConnections/CloudConnectionsSection'
import NodeLinkList from './nodeLinkStore/NodeLinkList'
import { useNetworksTabs } from './networksTabs'

/* Directory > Networks, at `/networks` (`routeGroups.ts`'s `NETWORKS_TAB_PATHS`): cloud connections and the org's
 * NodeLinks, the tab in `?tab=` (`networksTabs.ts` holds the table and gates). The route guards `nodes:view`
 * (`routesCloud.tsx`). One tab offered, no strip: the Directory strip already names the page; none, an empty state. */
export default function NetworksPage() {
  const tabs = useNetworksTabs()
  const [tab, selectTab] = useUrlTab(tabs, tabs[0]?.id ?? 'nodelink')
  // A shared link from someone offered more: say so rather than draw an empty page.
  if (tabs.length === 0) return <EmptyState title="No networks to show" />
  return (
    <Box data-testid="networks-page" sx={NODES_LIST_CONTAINER_SX}>
      {tabs.length > 1 && (
        <Box sx={PHONE_TAB_STRIP_SX}>
          <Tabs tabs={tabs} tab={tab} onSelect={selectTab} label="Network sections" idPrefix="networks" wrapOnPhone />
        </Box>
      )}
      {/* Each fetches on mount, so each mounts only while it is the tab on screen. */}
      {tabs.map(t => (
        <TabPanel key={t.id} id={t.id} current={tab} idPrefix="networks" label={tabs.length > 1 ? undefined : t.label}>
          {tab === t.id && (t.id === 'connections' ? <CloudConnectionsSection /> : <NodeLinkList />)}
        </TabPanel>
      ))}
    </Box>
  )
}
