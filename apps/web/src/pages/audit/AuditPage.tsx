import { Suspense, useCallback, useEffect, useRef, useState, type ComponentType } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import Box from '@mui/material/Box'
import { type TabDef, TabbedPanels } from '../../components/Tabs'
import { useAuth, useCan } from '../../auth'
import { isOrgAdminSeat } from '../../lib/orgAdmin'
import { NotOnPlan, planSurfaceShown } from '../../lib/planSwitches'
import {
  AUDIT_NODE_TAB_PATHS,
  AUDIT_SESSION_TAB_PATHS,
  AUDIT_TAB_PATHS,
  AUDIT_WORKFLOW_TAB_PATHS,
  normalizePathname,
} from '../../routeGroups'
import type { PermissionKey } from '../../types'
import NodesTab from './NodesTab'
import ScheduleTab from './ScheduleTab'
import SessionsTab from './SessionsTab'
import SlotIntentsTab from './SlotIntentsTab'
import UsersTab from './UsersTab'
import WorkflowRunsPanel from './WorkflowRunsPanel'
import ClientErrorsTab from './ClientErrorsTab'
import MemoryTab from './MemoryTab'
import { memoryOffered, mergeLanesOffered } from './projectAuditTabs'
import PersonasTab from '../personas/PersonasTab'
import TenancyExclusionsPage from '../tenancy/TenancyExclusionsPage'
import WorkflowTypesTab from '../workflows/WorkflowTypesTab'
import { DenyListBackground, useListBackground } from '../../components/ListDetail'
import { isEnabled } from '../../lib/editionFeatures'
import { editionModule } from '../../lib/editionModule'
import { lazyRetry as lazy } from '../../lib/lazyRetry'
import LoadingState from '../../ui/LoadingState'

/* Automations and Slots are whole pages of their own, loaded when their tab first opens. */
const AutomationList = lazy(() => import('../automations/AutomationList'))
const Worktrees = lazy(() => import('../Worktrees'))

/* Audit — the console's ONE "what happened" surface, in twelve tabs, three with a second strip.
 *
 *      Activity     /audit                        who did what through the API (tab id `users`)
 *      Nodes        /audit/nodes                  what the fleet was told to do
 *        · Commands     /audit/nodes                  every command dispatched to a node
 *        · Slot Intents /audit/nodes/slot-intents     every order standing against a pool slot
 *      Sessions     /sessions                     every agentic session, and its transcript
 *        · Sessions     /sessions                     the ledger — one row per session
 *        · Efficiency   /sessions/efficiency          where a period's runner time actually went
 *      Workflows    /audit/workflows              what this control plane ran on its own
 *        · Runs         /audit/workflows              every durable-workflow run
 *        · Schedule     /audit/workflows/schedule     every firing of every cron
 *        · Catalogue    /audit/workflows/catalogue    the durable workflows this CP can run
 *      Personas     /audit/personas               every role a fleet runner can be dispatched as
 *      Exclusions   /audit/exclusions             what the tenancy read seam held back
 *      Control plane /audit/control-plane         the control plane's own ECS service: deployments, and why tasks stopped
 *      Client errors /audit/client-errors         console errors and stalls reported by flagged orgs and users
 *      Automations  /automations                  every schedule and event trigger in the org (org admins)
 *      Slots        /worktrees                    the worktree pool: which task holds each slot (the operator's)
 *      Merge lanes  /audit/merge-lanes            every viewable project's merge lane rows; `?project=` opens one lane
 *      Memory       /audit/memory                 what runners learned, every viewable project; `?project=` opens one
 *
 * One surface because an operator asking "what happened" does not know which ledger holds the answer
 * (Automations are the schedule; the rest is what ran, and the catalogues it ran from). The populations are
 * NOT merged: every leaf has its own shape, and a common row would drop the columns that make each worth
 * reading. Efficiency is an aggregate over transcripts in object storage (different store, per-scan cost,
 * a "not configured" state), hence a sub-tab rather than part of the ledger.
 *
 * THE TAB IS THE PATHNAME, sub-tab too, so each leaf is a real address and `/sessions` keeps its
 * (`/sessions?task=<id>`, bookmarks, e2e specs). `routeGroups.ts` is the single home for which addresses
 * belong here and why the error boundary sees them as ONE page. A sub-strip's first tab IS its parent's
 * address, so a top-level click always lands on the first sub-tab (no history-dependent links).
 *
 * PERMISSION: one key per tab (`apps/api/app/authz/`'s "THE TIER KEY BECOMES THE DOOR"), or a seat where
 * the route reads one (Workflows' leaves, Personas, Exclusions, Control plane); Sessions also asks its plan switch
 * (`session_transcripts_enabled`; `lib/planSwitches.tsx`), Activity says "not on your plan" in its panel when
 * `audit_enabled` is off, and Nodes,
 * Workflows and Personas are the operator's; a holder sees
 * EVERYONE's activity within a tab (D4). `visibleTabs` decides visibility on TWO doors — the route guard
 * (App.tsx) and the panel's read key — or the strip advertises a dead end.
 */

type AuditTab =
  | 'users' | 'nodes' | 'sessions' | 'workflows' | 'personas' | 'exclusions' | 'control-plane' | 'client-errors'
  | 'automations' | 'slots' | 'merge-lanes' | 'memory'
type NodeTab = 'commands' | 'slot-intents'
type SessionTab = 'list' | 'efficiency'
type WorkflowTab = 'runs' | 'schedule' | 'catalogue'

/** The sixteen leaves — what the shell shows and latches. `sessions` is the ledger leaf's id (keyed by the
 *  latch and e2e specs); its sub-strip id is `list`, since strip ids name contents. */
type AuditLeaf =
  | 'users'
  | 'commands'
  | 'slot-intents'
  | 'sessions'
  | 'efficiency'
  | 'runs'
  | 'schedule'
  | 'catalogue'
  | 'personas'
  | 'exclusions'
  | 'control-plane'
  | 'client-errors'
  | 'automations'
  | 'slots'
  | 'merge-lanes'
  | 'memory'

/* Users is the default (the page's core question) and the bare `/audit`; an unknown pathname reads as it. */
const DEFAULT_TAB: AuditTab = 'users'

/* Each tab definition carries its PATH, intersected here rather than pushed into `components/Tabs.tsx`;
 * each list is the single id↔path table. Paths come from `routeGroups.ts`. */
type PathTabDef<Id extends string> = TabDef<Id> & { path: string }

/** A sub-tab also carries its panel's read key (as `pages/users/UsersPage.tsx`); each key was
 *  read off the route's own decorator and names its module. */
type KeyedTabDef<Id extends string> = PathTabDef<Id> & { permission: PermissionKey }

/* Incumbents keep their positions; newcomers append (muscle memory). */
const TABS: readonly PathTabDef<AuditTab>[] = [
  { id: 'users', label: 'Activity', path: AUDIT_TAB_PATHS.users },
  { id: 'nodes', label: 'Nodes', path: AUDIT_TAB_PATHS.nodes },
  { id: 'sessions', label: 'Sessions', path: AUDIT_TAB_PATHS.sessions },
  { id: 'workflows', label: 'Workflows', path: AUDIT_TAB_PATHS.workflows },
  { id: 'personas', label: 'Personas', path: AUDIT_TAB_PATHS.personas },
  { id: 'exclusions', label: 'Exclusions', path: AUDIT_TAB_PATHS.exclusions },
  { id: 'control-plane', label: 'Control plane', path: AUDIT_TAB_PATHS.controlPlane },
  { id: 'client-errors', label: 'Client errors', path: AUDIT_TAB_PATHS.clientErrors },
  { id: 'automations', label: 'Automations', path: AUDIT_TAB_PATHS.automations },
  { id: 'slots', label: 'Slots', path: AUDIT_TAB_PATHS.slots },
  { id: 'merge-lanes', label: 'Merge lanes', path: AUDIT_TAB_PATHS.mergeLanes },
  { id: 'memory', label: 'Memory', path: AUDIT_TAB_PATHS.memory },
]

/** Nodes' strip; `commands` IS `/audit/nodes` (bookmarks). Two keys:
 *  `GET /api/command-log` is `@feature("system", "view")` (`routes/portfolio.py`), `GET /api/nodes/intents`
 *  is `@feature("nodes", "view")` (`routes/nodes/intents.py`) — hence the filter. */
const NODE_TABS: readonly KeyedTabDef<NodeTab>[] = [
  { id: 'commands', label: 'Commands', path: AUDIT_NODE_TAB_PATHS.commands, permission: 'system:view' },
  {
    id: 'slot-intents',
    label: 'Intents',
    path: AUDIT_NODE_TAB_PATHS['slot-intents'],
    permission: 'nodes:view',
  },
]

/* Efficiency (`session_efficiency`) and Control plane (`control_plane_view`), loaded where present
   (`lib/editionModule.ts`). */
const EfficiencyTab = editionModule(isEnabled('session_efficiency'),
  () => import.meta.glob<ComponentType>('./EfficiencyTab.tsx', { eager: true, import: 'default' }))
const ControlPlaneTab = editionModule(isEnabled('control_plane_view'),
  () => import.meta.glob<ComponentType>('./ControlPlaneTab.tsx', { eager: true, import: 'default' }))
/* Merge lanes, with the lane (`merge_lane`). */
const MergeLanesTab = editionModule(isEnabled('merge_lane'),
  () => import.meta.glob<ComponentType>('./MergeLanesTab.tsx', { eager: true, import: 'default' }))

/** Sessions' strip; `list` IS `/sessions` (so `/sessions?task=<id>` lands on the ledger). Both leaves ask
 *  `@feature("sessions", "view")` (`routes/sessions.py`), still spelled per leaf so one can move. */
const SESSION_TABS: readonly KeyedTabDef<SessionTab>[] = [
  { id: 'list', label: 'Sessions', path: AUDIT_SESSION_TAB_PATHS.list, permission: 'sessions:view' },
  ...(EfficiencyTab ? [{
    id: 'efficiency',
    label: 'Efficiency',
    path: AUDIT_SESSION_TAB_PATHS.efficiency,
    permission: 'sessions:view',
  } as const] : []),
]

/** Workflows' strip; `runs` IS `/audit/workflows`. No `permission`: each leaf reads a seat, not a key —
 *  Runs and Catalogue the operator (`routes/workflows.py`, `@superadmin_only(Tenancy.OPERATOR)`), Schedule
 *  an org admin (`routes/crons.py`, `@org_admin`). */
const WORKFLOW_TABS: readonly PathTabDef<WorkflowTab>[] = [
  { id: 'runs', label: 'Runs', path: AUDIT_WORKFLOW_TAB_PATHS.runs },
  { id: 'schedule', label: 'Schedule', path: AUDIT_WORKFLOW_TAB_PATHS.schedule },
  { id: 'catalogue', label: 'Catalogue', path: AUDIT_WORKFLOW_TAB_PATHS.catalogue },
]

/* `hooks/useListPage`'s keys (`<panel>_page`, `<panel>_per`): each names where ONE latched panel was left, so
 * they travel with every tab trip while the panels' own filters (`?task=`, `?node=`…) do not. */
const PAGING_KEY = /_(?:page|per)$/

/** `search` split by that rule: `paging` the page keys, `rest` everything else (a query string, no `?`). */
function splitSearch(search: string): { paging: URLSearchParams; rest: string } {
  const paging = new URLSearchParams()
  const rest = new URLSearchParams()
  for (const [key, value] of new URLSearchParams(search)) (PAGING_KEY.test(key) ? paging : rest).append(key, value)
  return { paging, rest: rest.toString() }
}

/** `path` carrying `search`'s page keys, plus `own` (a panel's own query, as `splitSearch` gave it). */
function addressOf(path: string, search: string, own = ''): string {
  const { paging } = splitSearch(search)
  for (const [key, value] of new URLSearchParams(own)) paging.set(key, value)
  const query = paging.toString()
  return query ? `${path}?${query}` : path
}

/** A tab id every `case` above has named: a new one is a compile error here, never an empty panel. */
function never(id: never): never {
  throw new Error(`unhandled audit tab ${String(id)}`)
}

export default function AuditPage() {
  /* The URL is the tab (no state copy). Normalized first: the router renders this for `/audit/nodes/` but
     keeps the trailing slash in `location.pathname` (`routeGroups.ts` owns the trim). */
  const location = useLocation()
  const navigate = useNavigate()
  const pathname = normalizePathname(location.pathname)

  const { admin, resolving } = useAuth()
  const can = useCan()
  const isOperator = admin?.is_operator === true
  const isOrgAdmin = isOrgAdminSeat(admin)

  /* Sub-strips filtered first: a container tab's visibility is DERIVED from its leaves, never re-declared.
     Unmemoized: a tiny filter over a frozen list, and `useCan` is memoized. */
  const visibleNodeTabs = NODE_TABS.filter(tab => can(tab.permission))
  const visibleSessionTabs = SESSION_TABS.filter(tab => can(tab.permission))
  const visibleWorkflowTabs = WORKFLOW_TABS.filter(tab => (tab.id === 'schedule' ? isOrgAdmin : isOperator))

  /* WHICH TABS THIS READER IS OFFERED — every clause asks BOTH doors (route guard and read key); this shell
     sits under two guards (any of `audit:view`/`sessions:view`/`tasks:create` over `/audit*`, `sessions:view`
     over `/sessions*`).
     Sessions also asks its plan switch (`planSurfaceShown`, twin of App.tsx's `RequirePlanSwitch`); Activity is
     offered off the plan too, its panel saying so.
     Nodes, Workflows and Personas are the operator's (App.tsx's `RequireSuperadmin`), on top of their keys and
     seats. Workflows reads seats, never a key, per leaf (`WORKFLOW_TABS`): Runs and Catalogue the operator,
     Schedule an org admin. Personas asks `tasks:create` (`routes/personas.py`). Exclusions is the
     operator's (a `Tenancy.OPERATOR` route; App.tsx's `RequireSuperadmin`); so is Control plane
     (`routes/cp_ecs.py`: `require_operator`, read key `system:view`).
     Nodes degrades PER SUB-TAB rather than demanding both keys — requiring both would strip the command log
     from a `system:view`-only operator and the intent queue from a `nodes:view`-only one. Honest because the
     split falls on an existing sub-tab boundary; a lone survivor keeps a one-tab strip.
     Inside Commands, `NodesTab.tsx`'s node lookup (`GET /api/nodes`, `nodes:view`) decoration is missing for a
     `system:view`-only reader, and its guard skips the fetch — no 403 loop. */
  const visibleTabs = TABS.filter(tab => {
    switch (tab.id) {
      // Off the plan, Activity is still offered and says so in its panel, rather than vanishing: the Audit row
      // would otherwise land, unexplained, on whichever tab comes next.
      case 'users': return can('audit:view')
      // The community edition has one machine and one org, on loopback: no node command log, no tenancy seam to audit,
      // no ECS control plane.
      case 'nodes': return can('audit:view') && visibleNodeTabs.length > 0 && isOperator && isEnabled('cloud_nodes')
      case 'sessions': return visibleSessionTabs.length > 0 && planSurfaceShown(admin, 'session_transcripts_enabled')
      case 'workflows': return can('audit:view') && visibleWorkflowTabs.length > 0 && isOperator
      case 'personas': return can('tasks:create') && isOperator
      case 'exclusions': return can('audit:view') && isOperator && isEnabled('multi_org_tenancy')
      case 'control-plane': return can('audit:view') && can('system:view') && isOperator && ControlPlaneTab !== undefined
      // `GET /api/client-events` is `superadmin_only`; the route guard is `RequireSuperadmin`.
      case 'client-errors': return can('audit:view') && isOperator
      // Every `/api/automations` route is `@org_admin`; App.tsx's `RequireSuperadmin orgAdmin`.
      case 'automations': return isOrgAdmin
      // `/api/worktrees` asks `worktrees:view`; the route guard is `RequireSuperadmin`.
      case 'slots': return can('worktrees:view') && isOperator
      // `projectAuditTabs.ts`, the rule the rail's Audit row reads too; App.tsx's `RequireFeature permission="projects:view"`.
      case 'merge-lanes': return mergeLanesOffered(can) && MergeLanesTab !== undefined
      case 'memory': return memoryOffered(admin, can)
    }
  })

  /* The active top-level tab: prefix match for containers, equality for leaves, via an explicit search
     per strip (`/audit` prefixes every address). Resolved against VISIBLE sub-tabs, so e.g. a
     `nodes:view`-only reader redirected to `/audit/nodes` opens Slot Intents, not a blank panel. The
     literal tail is unreachable and kept as a harmless default rather than an assertion. */
  const activeNodeTab: NodeTab =
    visibleNodeTabs.find(t => t.path === pathname)?.id ?? visibleNodeTabs[0]?.id ?? 'commands'
  const activeSessionTab: SessionTab =
    visibleSessionTabs.find(t => t.path === pathname)?.id ?? visibleSessionTabs[0]?.id ?? 'list'
  const activeWorkflowTab: WorkflowTab =
    visibleWorkflowTabs.find(t => t.path === pathname)?.id ?? visibleWorkflowTabs[0]?.id ?? 'runs'
  /* Full sub-table, but only for an offered tab: `/audit/nodes` still selects Nodes for a reader denied
     Commands, while a wholly withheld tab selects nothing. */
  const offered = (id: AuditTab) => visibleTabs.some(t => t.id === id)
  const pathTab: AuditTab | null =
    visibleTabs.find(t => t.path === pathname)?.id
    ?? (offered('nodes') && NODE_TABS.some(t => t.path === pathname) ? 'nodes' : null)
    ?? (offered('sessions') && SESSION_TABS.some(t => t.path === pathname) ? 'sessions' : null)
    ?? (offered('workflows') && WORKFLOW_TABS.some(t => t.path === pathname) ? 'workflows' : null)

  /* The default is the first OFFERED tab (Activity for `audit:view`): a `sessions:view`-only reader at
     `/sessions` has no Activity tab. */
  const activeTab: AuditTab = pathTab ?? visibleTabs[0]?.id ?? DEFAULT_TAB

  /** The leaf: what the latches key on. */
  const activeLeaf: AuditLeaf =
    activeTab === 'nodes' ? activeNodeTab
      : activeTab === 'workflows' ? activeWorkflowTab
        /* The one place the `sessions` leaf and the `list` sub-tab ids meet (see `AuditLeaf`). */
        : activeTab === 'sessions' ? (activeSessionTab === 'efficiency' ? 'efficiency' : 'sessions')
          : activeTab

  /* Activity's last query while it was the address — what `selectTab` returns it to. */
  const activitySearch = useRef('')
  if (activeTab === 'users' && pathname === AUDIT_TAB_PATHS.users) {
    activitySearch.current = splitSearch(location.search).rest
  }

  /* `replace: true`: `Tabs` selects on FOCUS, so arrow keys would flood history. The query is DROPPED on a
     click: each URL filter (`?task=`, `?node=`, `?workflow=`, `?cron=`…) belongs to ONE panel and would
     re-apply later. Arriving deep links keep theirs; non-URL filters survive via the latches — and so does
     each panel's page, whose keys (`hooks/useListPage`, one prefix per panel) are CARRIED across every trip. */
  const search = location.search
  const selectTab = useCallback((next: AuditTab) => {
    const target = TABS.find(t => t.id === next)
    /* Activity is the exception: its filters ARE its query (`UsersTab.tsx`), so a trip away and back
       returns to the view it left, as the latched local-state panels do. */
    if (target) navigate(addressOf(target.path, search, next === 'users' ? activitySearch.current : ''), { replace: true })
  }, [navigate, search])

  /* Sub-strips navigate on the same terms; two small callbacks, each over its own table. */
  const selectNodeTab = useCallback((next: NodeTab) => {
    const target = NODE_TABS.find(t => t.id === next)
    if (target) navigate(addressOf(target.path, search), { replace: true })
  }, [navigate, search])

  const selectSessionTab = useCallback((next: SessionTab) => {
    const target = SESSION_TABS.find(t => t.id === next)
    if (target) navigate(addressOf(target.path, search), { replace: true })
  }, [navigate, search])

  const selectWorkflowTab = useCallback((next: WorkflowTab) => {
    const target = WORKFLOW_TABS.find(t => t.id === next)
    if (target) navigate(addressOf(target.path, search), { replace: true })
  }, [navigate, search])

  /* Behind an open editor (components/ListDetail.tsx) the shell never re-addresses itself: a hop to another
     of its paths would replace the editor's URL. The hop below stands down there. */
  const background = useListBackground()

  /* The address follows the leaf actually SHOWN (route guards are coarser than leaves, so a refused leaf's
     address is reachable); otherwise the URL lies about the screen (`UsersPage` makes the same argument).
     The query is carried (same surface, re-addressed); converges after one hop. */
  const shownPath =
    activeTab === 'nodes' ? NODE_TABS.find(t => t.id === activeNodeTab)?.path
      : activeTab === 'sessions' ? SESSION_TABS.find(t => t.id === activeSessionTab)?.path
        : activeTab === 'workflows' ? WORKFLOW_TABS.find(t => t.id === activeWorkflowTab)?.path
          : TABS.find(t => t.id === activeTab)?.path
  const correctedPath = shownPath !== undefined && shownPath !== pathname ? shownPath : null
  useEffect(() => {
    if (correctedPath === null || background) return
    navigate({ pathname: correctedPath, search: location.search }, { replace: true })
  }, [correctedPath, location.search, navigate, background])

  /* WHICH PANELS HAVE EVER BEEN OPENED — one latch per LEAF, since each leaf reads: without them any
     arrival fetches all eleven feeds, three of them aggregate scans (one billed by the byte). STICKY: a trip
     away and back re-fetches nothing and keeps filters. Seeded from `activeLeaf`, so the arriving read starts
     on the first render and no default panel latches open behind a redirect. Cost accepted: opened panels keep their streams and polls running while
     hidden; if that ever matters, gate that panel's SUBSCRIPTION on `activeLeaf`, never reset the latch. */
  const [opened, setOpened] = useState<ReadonlySet<AuditLeaf>>(() => new Set([activeLeaf]))
  useEffect(() => {
    setOpened(prev => (prev.has(activeLeaf) ? prev : new Set(prev).add(activeLeaf)))
  }, [activeLeaf])

  /* NO TAB OFFERED — never a blank shell. The route guard admits on any of three keys, so a reader can arrive
     with every tab withheld. Sessions, when the reader's keys would open it but the plan leaves it out, says "not
     on your plan" (`RequirePlanSwitch`'s panel; Activity says so in its own panel); anything else is a permission refusal, which takes
     the console's usual `/no-access` (`components/RequireFeature.tsx`'s terms, holding while `resolving`). */
  if (visibleTabs.length === 0) {
    if (visibleSessionTabs.length > 0) return <NotOnPlan name="session_transcripts_enabled" />
    if (resolving) return null
    return background ? <DenyListBackground deny={background} /> : <Navigate to="/no-access" replace />
  }

  return (
    <Box>
      {/* No header: the sidebar's Audit and the selected tab name the page, and every control belongs to one
          panel. */}
      {/* Only tabs this reader may open; a single one still gets its strip. Offered panels are always mounted
          (`hidden` when inactive, so `aria-controls` resolve), with their content rendered once latched and never
          unmounted. Panels NOT offered are not rendered at all (nothing to unhide in devtools, no reads). The
          server enforces it (`components/RequireFeature.tsx`). */}
      <TabbedPanels tabs={visibleTabs} tab={activeTab} onSelect={selectTab} label="Audit sections" idPrefix="audit">
        {({ id }) => {
          switch (id) {
            case 'users': return opened.has('users') && (planSurfaceShown(admin, 'audit_enabled') ? <UsersTab /> : <NotOnPlan name="audit_enabled" />)
            /* NODES — a container. Its own `idPrefix` is load-bearing: `components/Tabs.tsx` mints
               `${idPrefix}-tab-<id>`/`-panel-<id>`, and a nested strip under the outer prefix could point a tab at
               the panel containing it. The strip the re-key split (`NODE_TABS`): a one-key reader gets a one-tab
               strip. */
            case 'nodes': return (
              <TabbedPanels tabs={visibleNodeTabs} tab={activeNodeTab} onSelect={selectNodeTab}
                label="Node activity sections" idPrefix="audit-nodes">
                {leaf => {
                  switch (leaf.id) {
                    case 'commands': return opened.has('commands') && <NodesTab />
                    case 'slot-intents': return opened.has('slot-intents') && <SlotIntentsTab />
                    default: return never(leaf.id)
                  }
                }}
              </TabbedPanels>
            )
            /* SESSIONS — a container on the Nodes terms (`audit-sessions` prefix; `list` IS `/sessions`). Both
               leaves ask `sessions:view`, so it is two tabs or none (one without Efficiency). The per-leaf latch
               matters most here: `/sessions` must not trigger the byte-billed efficiency scan. */
            case 'sessions': return (
              <TabbedPanels tabs={visibleSessionTabs} tab={activeSessionTab} onSelect={selectSessionTab}
                label="Session sections" idPrefix="audit-sessions">
                {leaf => {
                  switch (leaf.id) {
                    case 'list': return opened.has('sessions') && <SessionsTab />
                    case 'efficiency': return opened.has('efficiency') && EfficiencyTab && <EfficiencyTab />
                    default: return never(leaf.id)
                  }
                }}
              </TabbedPanels>
            )
            /* WORKFLOWS — built only when offered, and each leaf only for its seat: a hidden panel would still put
               a refused tab in the DOM. The tab and its route guard are the operator's. */
            case 'workflows': return (
              <TabbedPanels tabs={visibleWorkflowTabs} tab={activeWorkflowTab} onSelect={selectWorkflowTab}
                label="Workflow activity sections" idPrefix="audit-workflows">
                {leaf => {
                  switch (leaf.id) {
                    /* Not forked: `WorkflowRunsPanel` supplies the workflow-type catalogue (see that file). */
                    case 'runs': return opened.has('runs') && <WorkflowRunsPanel />
                    case 'schedule': return opened.has('schedule') && <ScheduleTab />
                    /* `pt: 1` as `WorkflowRunsPanel`, so switching sub-tabs does not shift the first row. */
                    case 'catalogue': return <Box sx={{ pt: 1 }}><WorkflowTypesTab enabled={opened.has('catalogue')} /></Box>
                    default: return never(leaf.id)
                  }
                }}
              </TabbedPanels>
            )
            /* PERSONAS (the operator's, with `tasks:create`) and EXCLUSIONS (operator-only) — built only when offered. */
            case 'personas': return <PersonasTab enabled={opened.has('personas')} />
            /* The page keeps its seam badge in the panel. */
            case 'exclusions': return opened.has('exclusions') && <TenancyExclusionsPage />
            /* CONTROL PLANE (operator-only): the CP's own ECS service. */
            case 'control-plane': return opened.has('control-plane') && ControlPlaneTab && <ControlPlaneTab />
            /* CLIENT ERRORS (operator-only): what flagged consoles reported. */
            case 'client-errors': return <ClientErrorsTab enabled={opened.has('client-errors')} />
            /* AUTOMATIONS (org admins) and SLOTS (the operator's): whole pages, lazy, mounted once opened. */
            case 'automations': return opened.has('automations') && <Box sx={{ pt: 1 }}><Suspense fallback={<LoadingState rows={5} />}><AutomationList /></Suspense></Box>
            case 'slots': return opened.has('slots') && <Box sx={{ pt: 1 }}><Suspense fallback={<LoadingState rows={5} />}><Worktrees /></Suspense></Box>
            /* MERGE LANES and MEMORY (`projects:view`): org-wide, a project filter in `?project=`. */
            case 'merge-lanes': return opened.has('merge-lanes') && MergeLanesTab && <MergeLanesTab />
            case 'memory': return opened.has('memory') && <MemoryTab />
            default: return never(id)
          }
        }}
      </TabbedPanels>
    </Box>
  )
}
