import { Link as RouterLink, useLocation, useNavigate } from 'react-router-dom'
import MuiLink from '@mui/material/Link'
import { useAuth, useCan, useCanOnAnyProject } from '../../auth'
import { SUBSCRIPTION_PAGE } from '../../lib/companySetup'
import { type TabGate, useShownTabs } from '../../lib/gatedTabs'
import { isOrgAdminSeat } from '../../lib/orgAdmin'
import { useOffPlanArrival } from '../../lib/planSwitches'
import { cellOn } from '../../lib/providerGrid'
import { AGENTS_TAB_PATHS, normalizePathname } from '../../routeGroups'
import Banner from '../../ui/Banner'

/* ── CUSTOMIZE > AGENTS' SUB-TABS — the single table of its tabs: label, address and gate (`lib/gatedTabs.ts`) ──
   Read by the strip (`components/RouteTabLayout.tsx`'s `AgentsLayout`), which draws it once over every sub-tab's
   page, nested under the Customize strip. CUSTOM AGENTS (`/agents`, the org's own SDK agents: `AgentsPage.tsx`),
   CATALOG (`/agents/catalog`, approved connectors an org turns on for itself: `CatalogPage.tsx`) and REGISTRY
   (`/agents/registry`, ACP registry agents an org admin installs, pinned: `RegistryPage.tsx`). All read under
   `agents:view`, the key `apps/api/app/routes/agents.py` declares; Custom agents only while the provider grid has
   Agents on, Registry only while it also has an `acp:` row on (`acp_registry`), Catalog (the agent marketplace)
   always; the Customize tab (`lib/customizeTabs.ts`) and the routes in `App.tsx` carry the same gates. The runner
   accounts are Directory > AI Accounts (`/accounts`). */
export type AgentsTab = keyof typeof AGENTS_TAB_PATHS

type AgentsTabRow = TabGate & { id: AgentsTab; label: string; path: string }

const AGENTS_TABS: readonly AgentsTabRow[] = [
  { id: 'agents', label: 'Custom agents', path: AGENTS_TAB_PATHS.agents, requires: 'agents:view', planSwitch: 'agents' },
  { id: 'catalog', label: 'Catalog', path: AGENTS_TAB_PATHS.catalog, requires: 'agents:view' },
  { id: 'registry', label: 'Registry', path: AGENTS_TAB_PATHS.registry, requires: 'agents:view', planSwitch: ['agents', 'acp_registry'] },
]

/* EACH TAB'S VERB, ONE HOME: Agents' Register needs `agents:edit` somewhere; Catalog's Publish is the operator's;
   Registry has none. The pages gate their buttons on it. */
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
export function useAgentsTabs(): readonly AgentsTabRow[] {
  return useShownTabs(AGENTS_TABS)
}

/** The tab whose page this pathname is — exact, so an editor (`/agents/new`) is no tab's page. */
export function agentsTabAt(pathname: string): AgentsTabRow | undefined {
  const path = normalizePathname(pathname)
  return AGENTS_TABS.find(tab => tab.path === path)
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
