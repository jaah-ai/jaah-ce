import { useState } from 'react'
import Box from '@mui/material/Box'
import { TabActionsSlot } from '../../components/TabActionsSlot'
import { TabPanel } from '../../components/Tabs'
import Accounts from '../credentials/Accounts'
import { AgentsStrip, useAgentsTabs } from '../agents/agentsTabs'

/* Directory > Agents > Agents, at `/accounts` (`routeGroups.ts`'s `AGENTS_TAB_PATHS`): the runner account roster
 * under the Agents strip. The route guards `accounts:view` (App.tsx). Add account portals onto this strip's right
 * end. A viewer offered this tab alone gets no strip: one tab offers no choice, and the Directory strip already
 * names the page. */
export default function BrainsPage() {
  const tabs = useAgentsTabs()
  const [actionsSlot, setActionsSlot] = useState<HTMLElement | null>(null)
  if (tabs.length < 2) return <Accounts />
  return (
    <Box>
      <AgentsStrip tab="accounts" actions={<span ref={setActionsSlot} style={{ display: 'contents' }} />} />
      {/* Every other tab's `aria-controls` must resolve: an empty hidden panel each (they live at their own addresses). */}
      {tabs.filter(t => t.id !== 'accounts').map(t => (
        <TabPanel key={t.id} id={t.id} current="accounts" idPrefix="agents">{null}</TabPanel>
      ))}
      <TabPanel id="accounts" current="accounts" idPrefix="agents">
        {/* `undefined` until the strip's slot mounts: the header waits rather than jumping (`TabActionsSlot`). */}
        <TabActionsSlot.Provider value={actionsSlot ?? undefined}>
          <Accounts />
        </TabActionsSlot.Provider>
      </TabPanel>
    </Box>
  )
}
