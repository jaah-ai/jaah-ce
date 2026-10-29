/* WHICH PATHS ARE TABS OF ONE PAGE — single home for a fact the tab shells and `App.tsx`'s error
 * boundary must agree on. Three shells keep their tab in the PATHNAME (`pages/audit/AuditPage.tsx` and
 * `components/RouteTabLayout.tsx`'s two).
 * Keyed on the raw pathname, `AuthLayout`'s boundary would remount a whole shell on every tab click
 * (losing filters/scroll/sort and re-issuing reads), so it is
 * keyed (via its Suspense, `components/ListDetail.tsx`) on `pageKey`. Crash recovery across a shell's own addresses rides `App.tsx`'s separate
 * `resetOn={pathname}` (mount identity narrow, error lifetime wide).
 * The shells read their paths from here, so the boundary cannot fall out of step with the tab strips. */

/** Audit's six top-level tabs, in strip order (id → address). A tab with a second strip maps to that
 *  strip's DEFAULT sub-tab, so tab and front door are one address. */
export const AUDIT_TAB_PATHS = {
  users: '/audit',
  nodes: '/audit/nodes',
  sessions: '/sessions',
  workflows: '/audit/workflows',
  personas: '/audit/personas',
  exclusions: '/audit/exclusions',
  controlPlane: '/audit/control-plane',
} as const

/** Audit > Nodes' sub-tabs. `commands` IS `/audit/nodes` (bookmarks and the `/command-log` redirect land
 *  on it). */
export const AUDIT_NODE_TAB_PATHS = {
  commands: '/audit/nodes',
  'slot-intents': '/audit/nodes/slot-intents',
} as const

/** Audit > Sessions' sub-tabs. `list` IS `/sessions` (keeps `/sessions?task=<id>` deep links).
 *  Two sub-tabs because two populations: the ledger (CP database) and Efficiency (a windowed aggregate
 *  over transcripts in object storage, which can be unavailable, and costs money to compute). */
export const AUDIT_SESSION_TAB_PATHS = {
  list: '/sessions',
  efficiency: '/sessions/efficiency',
} as const

/** Audit > Workflows' sub-tabs; `runs` IS the parent's address. Separate because engine runs, schedule
 *  firings and the workflow catalogue share no row shape. */
export const AUDIT_WORKFLOW_TAB_PATHS = {
  runs: '/audit/workflows',
  schedule: '/audit/workflows/schedule',
  catalogue: '/audit/workflows/catalogue',
} as const

/** Directory > People's sub-tabs (`pages/users/UsersPage.tsx`), one address each — the original routes, and the
 *  viewer's own org's Access (who holds which org role; formerly Settings > Organizations' Access tab). */
export const USERS_TAB_PATHS = {
  users: '/users',
  groups: '/groups',
  access: '/access',
} as const

/** Directory > Machines' one ADDRESS (`pages/nodes/machinesTabs.tsx`): the node roster, whose other sub-tabs
 *  ride `?tab=` on it. */
export const MACHINES_TAB_PATHS = {
  nodes: '/nodes',
} as const

/** Directory > Networks' one ADDRESS (`pages/nodes/networksTabs.tsx`): cloud connections, NodeLink on `?tab=`. */
export const NETWORKS_TAB_PATHS = {
  networks: '/networks',
} as const

/** Directory > Agents' sub-tabs (`pages/agents/agentsTabs.tsx`): the runner account roster (labelled Agents; it
 *  KEEPS `/accounts` so bookmarks, the `/resources` redirect and its editors' list background
 *  (`lib/listDetail.ts`) all still resolve), the org's SDK agents (Custom agents), the connector catalog and
 *  the ACP registry. */
export const AGENTS_TAB_PATHS = {
  accounts: '/accounts',
  agents: '/agents',
  catalog: '/agents/catalog',
  registry: '/agents/registry',
} as const

/** Directory's tabs, in strip order (id → DEFAULT address). Each is its own page under one strip
 *  (`components/RouteTabLayout.tsx`); `lib/directoryTabs.ts` adds labels, gates and the further addresses a
 *  tab with sub-tabs answers on (People's `/groups`, Agents' `/agents`). */
export const DIRECTORY_TAB_PATHS = {
  projects: '/projects',
  nodes: MACHINES_TAB_PATHS.nodes,
  agents: AGENTS_TAB_PATHS.accounts,
  credentials: '/credentials',
  people: USERS_TAB_PATHS.users,
  networks: NETWORKS_TAB_PATHS.networks,
} as const

/** Automation's tabs (`components/RouteTabLayout.tsx`). The old Schedule (`/workflows`) and Triggers
 *  (`/triggers`) addresses redirect to `automations` (`pages/automations/LegacyRedirects.tsx`). Slots keeps
 *  `/worktrees`. */
export const AUTOMATION_TAB_PATHS = {
  automations: '/automations',
  playbooks: '/playbooks',
  worktrees: '/worktrees',
} as const

/** Task templates' addresses (`pages/templates/`): the gallery, the manage list and the editor's create page. Not
 *  tabs of one shell: each is its own page. `create` is static, so it ranks before the editor's `/templates/:id`. */
export const TEMPLATE_PATHS = {
  gallery: '/templates',
  manage: '/templates/manage',
  create: '/templates/new',
} as const

/* The first entry of each group is the key it answers to: the shell's DEFAULT address. */
const TAB_PATH_GROUPS: readonly (readonly string[])[] = [
  /* Every leaf of the Audit shell, sub-tabs included, collapses onto `/audit`. Repeated parent addresses
   * are harmless to `includes`. */
  [
    ...Object.values(AUDIT_TAB_PATHS),
    ...Object.values(AUDIT_NODE_TAB_PATHS),
    ...Object.values(AUDIT_SESSION_TAB_PATHS),
    ...Object.values(AUDIT_WORKFLOW_TAB_PATHS),
  ],
  /* Directory's list pages share one key so its strip is not rebuilt on every tab click — sub-tab
   * addresses included. */
  [
    ...Object.values(DIRECTORY_TAB_PATHS),
    ...Object.values(USERS_TAB_PATHS),
    ...Object.values(MACHINES_TAB_PATHS),
    ...Object.values(NETWORKS_TAB_PATHS),
    ...Object.values(AGENTS_TAB_PATHS),
  ],
  Object.values(AUTOMATION_TAB_PATHS),
]

/** A pathname reduced to the ONE spelling every lookup compares against: no trailing slash (except `/`).
 *  react-router matches `/groups/` to `/groups` but leaves the slash in `location.pathname`, and every
 *  table is exact-equality — un-normalized, `/groups/` rendered the DEFAULT tab (the account roster)
 *  with no error and churned the boundary. Normalized once, here. */
export function normalizePathname(pathname: string): string {
  const trimmed = pathname.replace(/\/+$/, '')
  /* `/` (and `//`) would trim to '', which is not an address. */
  return trimmed === '' ? '/' : trimmed
}

/** The PAGE a pathname belongs to: a shell's tab paths share one key; anything else is itself. Exact
 *  equality on the NORMALIZED path, never a prefix — `/groups/:id` and `/users/:id` are separate pages. */
export function pageKey(pathname: string): string {
  const path = normalizePathname(pathname)
  const group = TAB_PATH_GROUPS.find(paths => paths.includes(path))
  return group ? group[0] : path
}
