import { useAuth, useCan } from '../auth'
import type { PermissionKey } from '../types'
import { AUTOMATION_TAB_PATHS, normalizePathname } from '../routeGroups'
import { isOrgAdminSeat } from './orgAdmin'
import { type PlanSwitch, planSurfaceShown } from './planSwitches'

/* AUTOMATION — the single table of its tabs: label, address and gate. Read by the tab strip
 * (`components/RouteTabLayout.tsx`) and the rail's Automation row (`components/Sidebar.tsx`), which is
 * offered when ANY tab is and links to the first one offered.
 *
 * Each gate is the twin of that page's route guard in `App.tsx`: Automations is every org admin's (its API is
 * `@org_admin`, `routes/automations.py`); Playbooks, a preview sandbox, is read by every org member
 * (`routes/playbooks.py`) while the plan has Playbooks on, and always by the operator;
 * Slots (the worktree pool, last) is the operator's, with `worktrees:view`, the key `/api/worktrees` declares.
 * Presentation only, never the security boundary. */

export type AutomationTabId = keyof typeof AUTOMATION_TAB_PATHS

export type AutomationTab = {
  id: AutomationTabId
  label: string
  path: string
  operatorOnly?: boolean
  orgAdminOnly?: boolean
  requires?: PermissionKey
  /* Offered only while the org's plan has this switch on, or to the operator (`lib/planSwitches.ts`). */
  planSwitch?: PlanSwitch
}

export const AUTOMATION_TABS: readonly AutomationTab[] = [
  { id: 'automations', label: 'Automations', path: AUTOMATION_TAB_PATHS.automations, orgAdminOnly: true },
  { id: 'playbooks', label: 'Playbooks', path: AUTOMATION_TAB_PATHS.playbooks, planSwitch: 'playbooks_enabled' },
  { id: 'worktrees', label: 'Slots', path: AUTOMATION_TAB_PATHS.worktrees, operatorOnly: true, requires: 'worktrees:view' },
]

/** The tabs this viewer is offered, in strip order. */
export function useAutomationTabs(): readonly AutomationTab[] {
  const { admin } = useAuth()
  const can = useCan()
  return AUTOMATION_TABS.filter(
    tab => (!tab.operatorOnly || admin?.is_operator === true) && (!tab.orgAdminOnly || isOrgAdminSeat(admin))
      && (!tab.requires || can(tab.requires)) && (!tab.planSwitch || planSurfaceShown(admin, tab.planSwitch)),
  )
}

/** The tab whose page this pathname is — exact, so an editor (`/playbooks/12`) is no tab's page. */
export function automationTabAt(pathname: string): AutomationTab | undefined {
  const path = normalizePathname(pathname)
  return AUTOMATION_TABS.find(tab => tab.path === path)
}
