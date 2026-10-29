import { type ReactNode, useEffect } from 'react'
import { Link as RouterLink, useLocation, useNavigate } from 'react-router-dom'
import Box from '@mui/material/Box'
import MuiLink from '@mui/material/Link'
import { Tabs } from '../../components/Tabs'
import { useAuth, useCan, useCanOnAnyProject } from '../../auth'
import { SUBSCRIPTION_PAGE } from '../../lib/companySetup'
import { isOrgAdminSeat } from '../../lib/orgAdmin'
import { planSwitchOn, useOffPlanArrival } from '../../lib/planSwitches'
import { cellOn } from '../../lib/providerGrid'
import { AGENTS_TAB_PATHS } from '../../routeGroups'
import Banner from '../../ui/Banner'
import { PHONE, PHONE_TAB_STRIP_SX } from '../../ui/listStyles'

/* ── DIRECTORY > AGENTS' SUB-TABS ─────────────────────────────────────────────────────────────────────────
   AGENTS (`/accounts`, the runner accounts the fleet's runners log in as: `pages/nodes/BrainsPage.tsx`, under
   `accounts:view` whatever the plan), CUSTOM AGENTS (`/agents`, the org's own SDK agents: `AgentsPage.tsx`),
   CATALOG (`/agents/catalog`, approved connectors an org turns on for itself: `CatalogPage.tsx`) and REGISTRY
   (`/agents/registry`, ACP registry agents an org admin installs, pinned: `RegistryPage.tsx`). The last three read
   under `agents:view`, the key `apps/api/app/routes/agents.py` declares, while the provider grid has Agents on;
   the Directory tab (`lib/directoryTabs.ts`) and the routes in `App.tsx` carry the same gates. Selecting
   navigates (`replace`, like every strip that selects on focus). */
export type AgentsTab = keyof typeof AGENTS_TAB_PATHS

const AGENTS_TABS: readonly { id: AgentsTab; label: string }[] = [
  { id: 'accounts', label: 'Agents' },
  { id: 'agents', label: 'Custom agents' },
  { id: 'catalog', label: 'Catalog' },
  { id: 'registry', label: 'Registry' },
]

/* EACH TAB'S VERB, ONE HOME: Agents' Register needs `agents:edit` somewhere; Catalog's Publish is the operator's;
   Registry has none. The pages gate their buttons on it, and the strip keeps a phone's verb slot only for a viewer
   who can hold one. */
/** `canRegister` edits agents; `canAdd` also adds one (needs the `sdk` row on: the API refuses otherwise);
 *  `canPublish` publishes a connector (the operator, its own org's `sdk` row on). */
export function useAgentsVerbs(): { canRegister: boolean; canAdd: boolean; canPublish: boolean } {
  const { admin } = useAuth()
  const can = useCan()
  const canOnAny = useCanOnAnyProject()
  const canRegister = can('agents:edit') || canOnAny('agents:edit')
  const sdkOn = cellOn(admin, 'sdk', 'own')
  return { canRegister, canAdd: canRegister && sdkOn, canPublish: admin?.is_operator === true && sdkOn }
}

/** The sub-tabs this viewer is offered, in order — the twins of `App.tsx`'s route guards. */
export function useAgentsTabs() {
  const { admin } = useAuth()
  const can = useCan()
  const sdk = can('agents:view') && planSwitchOn(admin, 'agents')
  const shown: Record<AgentsTab, boolean> = {
    accounts: can('accounts:view'),
    agents: sdk,
    catalog: sdk,
    /* Registry is offered only while the provider grid has an `acp:` row on (`acp_registry`): off, no tab. */
    registry: sdk && planSwitchOn(admin, 'acp_registry'),
  }
  return AGENTS_TABS.filter(t => shown[t.id])
}

export function AgentsStrip({ tab, actions }: { tab: AgentsTab; actions?: ReactNode }) {
  const navigate = useNavigate()
  const { state } = useLocation()
  const { canRegister, canPublish } = useAgentsVerbs()
  const tabs = useAgentsTabs()
  /* Every pick mounts another page and this strip with it, so focus would drop to <body> mid-arrowing. The hop says
     so in its `state`, and the new strip takes focus back onto its selected tab. */
  const refocus = (state as { agentsStripFocus?: boolean } | null)?.agentsStripFocus === true
  useEffect(() => {
    if (refocus) document.getElementById(`agents-tab-${tab}`)?.focus()
    // On arrival only.
  }, [refocus])
  return (
    /* Unshrinkable tabs, so the fit's width probes measure whole labels (`components/RouteTabLayout.tsx`). */
    <Box sx={[PHONE_TAB_STRIP_SX, { [PHONE]: { '& .MuiTab-root': { flexShrink: 0 } } }]}>
      <Tabs
        tabs={tabs}
        tab={tab}
        onSelect={next => navigate(AGENTS_TAB_PATHS[next], { replace: true, state: { agentsStripFocus: true } })}
        label="Agent sections"
        idPrefix="agents"
        /* One line at every width, the tabs that do not fit behind More (`components/Tabs`): Agents' three verbs
           (Share, Import, ＋) leave a phone's row room for two tabs, and wrapping strands the rest on a line of its own. */
        overflowMenu
        /* The verb shares the strip's row (`ui/CompactVerb`). Agents' Add account always takes it; otherwise no
           verb on any tab for this viewer, no slot. */
        compactActions={tab === 'accounts' || canRegister || canPublish}
        actions={actions}
      />
    </Box>
  )
}

/* A reader sent here from `/agents/registry` while the plan leaves the registry off (`App.tsx`'s guard or the
   Registry page's own 403) is told why, once: the note lives on the hop's history state, so a tab switch or
   Dismiss drops it. The plan page is the org admin's (`RequireSuperadmin orgAdmin`); anyone else is pointed at one. */
export function RegistryOffPlanNote() {
  const navigate = useNavigate()
  const { pathname, search } = useLocation()
  const { admin } = useAuth()
  if (useOffPlanArrival() !== 'acp_registry') return null
  return (
    <Banner
      severity="info"
      testId="registry-off-plan"
      onDismiss={() => navigate({ pathname, search }, { replace: true, state: null })}
    >
      Registry needs a plan that includes it.{' '}
      {isOrgAdminSeat(admin)
        ? <MuiLink component={RouterLink} to={SUBSCRIPTION_PAGE}>See plans</MuiLink>
        : 'Ask an admin to change the plan.'}
    </Banner>
  )
}
