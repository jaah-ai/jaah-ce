import { type Admin, useAuth, useCan } from '../auth'
import { normalizePathname } from '../routeGroups'
import type { PermissionKey } from '../types'
import { type FeatureName, isEnabled } from './editionFeatures'
import { isOrgAdminSeat } from './orgAdmin'
import { type PlanSwitch, planSurfaceShown, planSwitchOn } from './planSwitches'

/* WHO IS OFFERED A TAB — the one home of the question every tab table asks (`lib/directoryTabs.ts`,
 * `lib/customizeTabs.ts`, `pages/agents/agentsTabs.tsx`, `pages/nodes/machinesTabs.tsx`, `pages/nodes/networksTabs.ts`).
 * A table row declares its gate; this answers it. Every term must hold; an absent term holds. Presentation only,
 * never the security boundary (`components/RequireFeature.tsx`): each table names its gates' route/API twins. */

export type Can = (key: PermissionKey) => boolean

export type TabGate = {
  /* Only in an edition that includes this feature (`lib/editionFeatures.ts`). */
  feature?: FeatureName
  operatorOnly?: boolean
  /* The tenant-administration seat (`lib/orgAdmin.ts`): the operator, or a tenant's Owner/Admin. */
  orgAdminOnly?: boolean
  /* The permission key(s) the viewer must hold. */
  requires?: PermissionKey | readonly PermissionKey[]
  /* The org's plan switch(es) that must be on, the operator's own org included (`planSwitchOn`). */
  planSwitch?: PlanSwitch | readonly PlanSwitch[]
  /* A plan feature's surface: on for the tenant whose plan has the switch, and always for the operator (`planSurfaceShown`). */
  planSurface?: PlanSwitch
  /* Whatever these terms cannot say, as the last term. */
  shown?: (admin: Admin | null | undefined, can: Can) => boolean
}

const each = <T,>(one: T | readonly T[] | undefined, holds: (t: T) => boolean): boolean =>
  one === undefined || (Array.isArray(one) ? (one as readonly T[]).every(holds) : holds(one as T))

/** Is this viewer offered a tab behind `gate`? Pure, so a table can answer it for a strip and a rail alike. */
export function tabShown(gate: TabGate, admin: Admin | null | undefined, can: Can): boolean {
  return (!gate.feature || isEnabled(gate.feature))
    && (!gate.operatorOnly || admin?.is_operator === true)
    && (!gate.orgAdminOnly || isOrgAdminSeat(admin))
    && each(gate.requires, can)
    && each(gate.planSwitch, name => planSwitchOn(admin, name))
    && (!gate.planSurface || planSurfaceShown(admin, gate.planSurface))
    && (!gate.shown || gate.shown(admin, can))
}

/** Each tab's answer by id. */
export function tabsShown<Id extends string>(
  tabs: readonly (TabGate & { id: Id })[], admin: Admin | null | undefined, can: Can,
): Record<Id, boolean> {
  return Object.fromEntries(tabs.map(t => [t.id, tabShown(t, admin, can)])) as Record<Id, boolean>
}

/** The rows of a tab table this viewer is offered, in table order. */
export function useShownTabs<T extends TabGate>(tabs: readonly T[]): T[] {
  const { admin } = useAuth()
  const can = useCan()
  return tabs.filter(t => tabShown(t, admin, can))
}

/* A ROUTE TAB TABLE (Directory, Customize): each tab answers on one or more addresses, each behind its own gate. */
export type RouteTab<Id extends string = string> = TabGate & {
  id: Id
  label: string
  /* The tab's addresses: its default first, then each sub-tab with an address of its own. The tab is offered when ANY
     is readable and links to the first readable one. */
  pages: readonly (TabGate & { path: string })[]
  /* Address prefixes its editors live under besides its pages (a pack's rule), for the rail's lit row. */
  under?: readonly string[]
}

/** The table trimmed to this edition's features at load, so the strip, `routeTabAt` and the rail agree. */
export const editionTabs = <T extends TabGate>(all: readonly T[]): readonly T[] =>
  all.filter(tab => !tab.feature || isEnabled(tab.feature))

/** The tabs this viewer is offered, in strip order; `path` is the first of a tab's pages that viewer may read. */
export function useOfferedTabs<T extends RouteTab>(tabs: readonly T[]): readonly (T & { path: string })[] {
  const { admin } = useAuth()
  const can = useCan()
  return tabs.flatMap(tab => {
    if (!tabShown(tab, admin, can)) return []
    const page = tab.pages.find(p => tabShown(p, admin, can))
    return page ? [{ ...tab, path: page.path }] : []
  })
}

/** The tab whose LIST page this pathname is — exact, so an editor (`/projects/12`) is no tab's page. */
export function routeTabAt<T extends RouteTab>(tabs: readonly T[], pathname: string): T | undefined {
  const path = normalizePathname(pathname)
  return tabs.find(tab => tab.pages.some(p => p.path === path))
}

/** Whether a pathname lies anywhere in the table — a list page or one of its editors (`/projects/12`,
 *  `/templates/manage`). The rail's row is active across all of them. */
export function isUnderTabs(tabs: readonly RouteTab[], pathname: string): boolean {
  const within = (path: string) => pathname === path || pathname.startsWith(`${path}/`)
  return tabs.some(tab => tab.pages.some(p => within(p.path)) || (tab.under ?? []).some(within))
}
