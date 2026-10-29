import { useCallback, useEffect, useRef, useState, type ComponentType } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import Box from '@mui/material/Box'
import { TabPanel, Tabs, type TabDef } from '../../components/Tabs'
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
import PersonasTab from '../personas/PersonasTab'
import TenancyExclusionsPage from '../tenancy/TenancyExclusionsPage'
import WorkflowTypesTab from '../workflows/WorkflowTypesTab'
import { DenyListBackground, useListBackground } from '../../components/ListDetail'
import { isEnabled } from '../../lib/editionFeatures'

/* Audit — the console's ONE "what happened" surface, in seven tabs, three with a second strip.
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
 *
 * One surface because an operator asking "what happened" does not know which ledger holds the answer
 * (Automation is the schedule; Audit is what ran, and the catalogues it ran from). The populations are
 * NOT merged: every leaf has its own shape, and a common row would drop the columns that make each worth
 * reading. Efficiency is an aggregate over transcripts in object storage (different store, per-scan cost,
 * a "not configured" state), hence a sub-tab rather than part of the ledger.
 *
 * THE TAB IS THE PATHNAME, sub-tab too, so each leaf is a real address and `/sessions` keeps its
 * (`/sessions?task=<id>`, bookmarks, e2e specs). `routeGroups.ts` is the single home for which addresses
 * belong here and why the error boundary sees them as ONE page. A sub-strip's first tab IS its parent's
 * address, so a top-level click always lands on the first sub-tab (no history-dependent links).
 *
 * LEGACY `?tab=` LINKS STILL RESOLVE (`/audit?tab=nodes`, from the retired `/command-log`): one hop onto
 * the path, other params preserved; see `legacyTab`.
 *
 * PERMISSION: one key per tab (`apps/api/app/authz/`'s "THE TIER KEY BECOMES THE DOOR"), or a seat where
 * the route reads one (Workflows' leaves, Personas, Exclusions, Control plane); Activity and Sessions also ask
 * their plan switch (`audit_enabled`, `session_transcripts_enabled`; `lib/planSwitches.tsx`), and Nodes,
 * Workflows and Personas are the operator's; a holder sees
 * EVERYONE's activity within a tab (D4). `visibleTabs` decides visibility on TWO doors — the route guard
 * (App.tsx) and the panel's read key — or the strip advertises a dead end.
 */

type AuditTab = 'users' | 'nodes' | 'sessions' | 'workflows' | 'personas' | 'exclusions' | 'control-plane'
type NodeTab = 'commands' | 'slot-intents'
type SessionTab = 'list' | 'efficiency'
type WorkflowTab = 'runs' | 'schedule' | 'catalogue'

/** The eleven leaves — what the shell shows and latches. `sessions` is the ledger leaf's id (keyed by the
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
]

/** Nodes' strip; `commands` IS `/audit/nodes` (bookmarks and the `/command-log` redirect). Two keys:
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

/* Efficiency (`session_efficiency`) and Control plane (`control_plane_view`), loaded where present: the community
   edition's tree leaves both out, and `import.meta.glob` yields `{}` for an absent file. */
const EfficiencyTab: ComponentType | undefined = Object.values(isEnabled('session_efficiency')
  ? import.meta.glob<ComponentType>('./EfficiencyTab.tsx', { eager: true, import: 'default' })
  : {})[0]
const ControlPlaneTab: ComponentType | undefined = Object.values(isEnabled('control_plane_view')
  ? import.meta.glob<ComponentType>('./ControlPlaneTab.tsx', { eager: true, import: 'default' })
  : {})[0]

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
     Activity and Sessions also ask their plan switch (`planSurfaceShown`, twin of App.tsx's `RequirePlanSwitch`).
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
      case 'users': return can('audit:view') && planSurfaceShown(admin, 'audit_enabled')
      // The community edition has one machine and one org, on loopback: no node command log, no tenancy seam to audit,
      // no ECS control plane.
      case 'nodes': return can('audit:view') && visibleNodeTabs.length > 0 && isOperator && isEnabled('cloud_nodes')
      case 'sessions': return visibleSessionTabs.length > 0 && planSurfaceShown(admin, 'session_transcripts_enabled')
      case 'workflows': return can('audit:view') && visibleWorkflowTabs.length > 0 && isOperator
      case 'personas': return can('tasks:create') && isOperator
      case 'exclusions': return can('audit:view') && isOperator && isEnabled('multi_org_tenancy')
      case 'control-plane': return can('audit:view') && can('system:view') && isOperator && ControlPlaneTab !== undefined
    }
  })

  /* The legacy `?tab=` arrival is resolved in the same expression as `activeTab`: child effects run
     before the parent's, so reading only the pathname would fire `<UsersTab/>`'s `GET /api/audit` (and
     latch it) before the redirect. Only on `/audit`; unknown values stay untouched; resolved against
     VISIBLE tabs, so no redirect lands on a refused address. */
  const legacyTab =
    pathname === AUDIT_TAB_PATHS.users
      ? visibleTabs.find(t => t.id === new URLSearchParams(location.search).get('tab')) ?? null
      : null

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
  const activeTab: AuditTab = legacyTab?.id ?? pathTab ?? visibleTabs[0]?.id ?? DEFAULT_TAB

  /** The leaf: what the latches key on. */
  const activeLeaf: AuditLeaf =
    activeTab === 'nodes' ? activeNodeTab
      : activeTab === 'workflows' ? activeWorkflowTab
        /* The one place the `sessions` leaf and the `list` sub-tab ids meet (see `AuditLeaf`). */
        : activeTab === 'sessions' ? (activeSessionTab === 'efficiency' ? 'efficiency' : 'sessions')
          : activeTab

  /* Activity's last query while it was the address — what `selectTab` returns it to. */
  const activitySearch = useRef('')
  if (activeTab === 'users' && legacyTab === null && pathname === AUDIT_TAB_PATHS.users) {
    activitySearch.current = location.search
  }

  /* `replace: true`: `Tabs` selects on FOCUS, so arrow keys would flood history. The query is DROPPED on a
     click: each URL filter (`?task=`, `?node=`, `?workflow=`, `?cron=`…) belongs to ONE panel and would
     re-apply later. Arriving deep links keep theirs; non-URL filters survive via the latches. */
  const selectTab = useCallback((next: AuditTab) => {
    const target = TABS.find(t => t.id === next)
    /* Activity is the exception: its filters ARE its query (`UsersTab.tsx`), so a trip away and back
       returns to the view it left, as the latched local-state panels do. */
    if (target) navigate(target.path + (next === 'users' ? activitySearch.current : ''), { replace: true })
  }, [navigate])

  /* Sub-strips navigate on the same terms; two small callbacks, each over its own table. */
  const selectNodeTab = useCallback((next: NodeTab) => {
    const target = NODE_TABS.find(t => t.id === next)
    if (target) navigate(target.path, { replace: true })
  }, [navigate])

  const selectSessionTab = useCallback((next: SessionTab) => {
    const target = SESSION_TABS.find(t => t.id === next)
    if (target) navigate(target.path, { replace: true })
  }, [navigate])

  const selectWorkflowTab = useCallback((next: WorkflowTab) => {
    const target = WORKFLOW_TABS.find(t => t.id === next)
    if (target) navigate(target.path, { replace: true })
  }, [navigate])

  /* The legacy `?tab=` hop — the address half of `legacyTab`. Every spelling is rewritten (even
     `?tab=users`, or it stays a second spelling of `/audit`). One `replace` hop that changes nothing on
     screen; only `tab` is deleted, the rest of the query survives. */
  const legacyTabPath = legacyTab?.path ?? null
  /* Behind an open editor (components/ListDetail.tsx) the shell never re-addresses itself: a hop to another
     of its paths would replace the editor's URL. Both hops below stand down there. */
  const background = useListBackground()
  useEffect(() => {
    if (legacyTabPath === null || background) return
    const params = new URLSearchParams(location.search)
    params.delete('tab')
    const rest = params.toString()
    navigate(rest ? `${legacyTabPath}?${rest}` : legacyTabPath, { replace: true })
  }, [legacyTabPath, location.search, navigate, background])

  /* The address follows the leaf actually SHOWN (route guards are coarser than leaves, so a refused leaf's
     address is reachable); otherwise the URL lies about the screen (`UsersPage` makes the same argument).
     The query is carried (same surface, re-addressed). Skipped while a legacy `?tab=` hop owns the address
     (two effects rewriting in opposite directions would loop); converges after one hop. */
  const shownPath =
    activeTab === 'nodes' ? NODE_TABS.find(t => t.id === activeNodeTab)?.path
      : activeTab === 'sessions' ? SESSION_TABS.find(t => t.id === activeSessionTab)?.path
        : activeTab === 'workflows' ? WORKFLOW_TABS.find(t => t.id === activeWorkflowTab)?.path
          : TABS.find(t => t.id === activeTab)?.path
  const correctedPath =
    legacyTab === null && shownPath !== undefined && shownPath !== pathname
      ? shownPath
      : null
  useEffect(() => {
    if (correctedPath === null || background) return
    navigate({ pathname: correctedPath, search: location.search }, { replace: true })
  }, [correctedPath, location.search, navigate, background])

  /* WHICH PANELS HAVE EVER BEEN OPENED — one latch per LEAF, since each leaf reads: without them any
     arrival fetches all eleven feeds, three of them aggregate scans (one billed by the byte). STICKY: a trip
     away and back re-fetches nothing and keeps filters. Seeded from `activeLeaf` (which already accounts
     for a legacy `?tab=`), so the arriving read starts on the first render and no default panel latches
     open behind a redirect. Cost accepted: opened panels keep their streams and polls running while
     hidden; if that ever matters, gate that panel's SUBSCRIPTION on `activeLeaf`, never reset the latch. */
  const [usersOpened, setUsersOpened] = useState(activeLeaf === 'users')
  const [commandsOpened, setCommandsOpened] = useState(activeLeaf === 'commands')
  const [slotIntentsOpened, setSlotIntentsOpened] = useState(activeLeaf === 'slot-intents')
  const [sessionsOpened, setSessionsOpened] = useState(activeLeaf === 'sessions')
  const [efficiencyOpened, setEfficiencyOpened] = useState(activeLeaf === 'efficiency')
  const [runsOpened, setRunsOpened] = useState(activeLeaf === 'runs')
  const [scheduleOpened, setScheduleOpened] = useState(activeLeaf === 'schedule')
  const [catalogueOpened, setCatalogueOpened] = useState(activeLeaf === 'catalogue')
  const [personasOpened, setPersonasOpened] = useState(activeLeaf === 'personas')
  const [exclusionsOpened, setExclusionsOpened] = useState(activeLeaf === 'exclusions')
  const [controlPlaneOpened, setControlPlaneOpened] = useState(activeLeaf === 'control-plane')
  useEffect(() => {
    if (activeLeaf === 'users') setUsersOpened(true)
    if (activeLeaf === 'commands') setCommandsOpened(true)
    if (activeLeaf === 'slot-intents') setSlotIntentsOpened(true)
    if (activeLeaf === 'sessions') setSessionsOpened(true)
    if (activeLeaf === 'efficiency') setEfficiencyOpened(true)
    if (activeLeaf === 'runs') setRunsOpened(true)
    if (activeLeaf === 'schedule') setScheduleOpened(true)
    if (activeLeaf === 'catalogue') setCatalogueOpened(true)
    if (activeLeaf === 'personas') setPersonasOpened(true)
    if (activeLeaf === 'exclusions') setExclusionsOpened(true)
    if (activeLeaf === 'control-plane') setControlPlaneOpened(true)
  }, [activeLeaf])

  /* NO TAB OFFERED — never a blank shell. The route guard admits on any of three keys, so a reader can arrive
     with every tab withheld. A tab the reader's keys would open but the plan leaves out says "not on your plan"
     (`RequirePlanSwitch`'s panel, Activity's switch first); anything else is a permission refusal, which takes
     the console's usual `/no-access` (`components/RequireFeature.tsx`'s terms, holding while `resolving`). */
  if (visibleTabs.length === 0) {
    if (can('audit:view') && !planSurfaceShown(admin, 'audit_enabled')) return <NotOnPlan name="audit_enabled" />
    if (visibleSessionTabs.length > 0) return <NotOnPlan name="session_transcripts_enabled" />
    if (resolving) return null
    return background ? <DenyListBackground deny={background} /> : <Navigate to="/no-access" replace />
  }

  return (
    <Box>
      {/* No header: the sidebar's Audit and the selected tab name the page, and every control belongs to one
          panel. */}
      {/* Only tabs this reader may open; a single one still gets its strip. */}
      <Tabs
        tabs={visibleTabs}
        tab={activeTab}
        onSelect={selectTab}
        label="Audit sections"
        idPrefix="audit"
      />

      {/* Offered panels are always mounted (`hidden` when inactive, so `aria-controls` resolve), with their
          content rendered once latched and never unmounted. Panels NOT offered are not rendered at all
          (nothing to unhide in devtools, no reads). The server enforces it (`components/RequireFeature.tsx`). */}
      {offered('users') && (
        <TabPanel id="users" current={activeTab} idPrefix="audit">
          {usersOpened && <UsersTab />}
        </TabPanel>
      )}

      {/* NODES — a container. Its own `idPrefix` is load-bearing: `components/Tabs.tsx` mints
          `${idPrefix}-tab-<id>`/`-panel-<id>`, and a nested strip under the outer prefix could point a tab at
          the panel containing it. */}
      {offered('nodes') && (
        <TabPanel id="nodes" current={activeTab} idPrefix="audit">
          {/* The strip the re-key split (`NODE_TABS`): a one-key reader gets a one-tab strip. */}
          <Tabs
            tabs={visibleNodeTabs}
            tab={activeNodeTab}
            onSelect={selectNodeTab}
            label="Node activity sections"
            idPrefix="audit-nodes"
          />
          {visibleNodeTabs.some(t => t.id === 'commands') && (
            <TabPanel id="commands" current={activeNodeTab} idPrefix="audit-nodes">
              {commandsOpened && <NodesTab />}
            </TabPanel>
          )}
          {visibleNodeTabs.some(t => t.id === 'slot-intents') && (
            <TabPanel id="slot-intents" current={activeNodeTab} idPrefix="audit-nodes">
              {slotIntentsOpened && <SlotIntentsTab />}
            </TabPanel>
          )}
        </TabPanel>
      )}

      {/* SESSIONS — a container on the Nodes terms (`audit-sessions` prefix; `list` IS `/sessions`). The
          per-leaf latch matters most here: `/sessions` must not trigger the byte-billed efficiency scan. */}
      {offered('sessions') && (
        <TabPanel id="sessions" current={activeTab} idPrefix="audit">
          {/* No member conditional: both leaves ask `sessions:view`, so it is two tabs or none (one without Efficiency). */}
          <Tabs
            tabs={visibleSessionTabs}
            tab={activeSessionTab}
            onSelect={selectSessionTab}
            label="Session sections"
            idPrefix="audit-sessions"
          />
          <TabPanel id="list" current={activeSessionTab} idPrefix="audit-sessions">
            {sessionsOpened && <SessionsTab />}
          </TabPanel>
          <TabPanel id="efficiency" current={activeSessionTab} idPrefix="audit-sessions">
            {efficiencyOpened && EfficiencyTab && <EfficiencyTab />}
          </TabPanel>
        </TabPanel>
      )}

      {/* WORKFLOWS — built only when offered, and each leaf only for its seat: a hidden panel would still put
          a refused tab in the DOM. The tab and its route guard are the operator's. */}
      {offered('workflows') && (
        <TabPanel id="workflows" current={activeTab} idPrefix="audit">
          <Tabs
            tabs={visibleWorkflowTabs}
            tab={activeWorkflowTab}
            onSelect={selectWorkflowTab}
            label="Workflow activity sections"
            idPrefix="audit-workflows"
          />
          {visibleWorkflowTabs.some(t => t.id === 'runs') && (
            <TabPanel id="runs" current={activeWorkflowTab} idPrefix="audit-workflows">
              {/* Not forked: `WorkflowRunsPanel` supplies the workflow-type catalogue (see that file). */}
              {runsOpened && <WorkflowRunsPanel />}
            </TabPanel>
          )}
          {visibleWorkflowTabs.some(t => t.id === 'schedule') && (
            <TabPanel id="schedule" current={activeWorkflowTab} idPrefix="audit-workflows">
              {scheduleOpened && <ScheduleTab />}
            </TabPanel>
          )}
          {visibleWorkflowTabs.some(t => t.id === 'catalogue') && (
            <TabPanel id="catalogue" current={activeWorkflowTab} idPrefix="audit-workflows">
              {/* `pt: 1` as `WorkflowRunsPanel`, so switching sub-tabs does not shift the first row. */}
              <Box sx={{ pt: 1 }}>
                <WorkflowTypesTab enabled={catalogueOpened} />
              </Box>
            </TabPanel>
          )}
        </TabPanel>
      )}

      {/* PERSONAS (the operator's, with `tasks:create`) and EXCLUSIONS (operator-only) — built only when offered. */}
      {offered('personas') && (
        <TabPanel id="personas" current={activeTab} idPrefix="audit">
          <PersonasTab enabled={personasOpened} />
        </TabPanel>
      )}

      {/* The page keeps its seam badge in the panel. */}
      {offered('exclusions') && (
        <TabPanel id="exclusions" current={activeTab} idPrefix="audit">
          {exclusionsOpened && <TenancyExclusionsPage />}
        </TabPanel>
      )}

      {/* CONTROL PLANE (operator-only): the CP's own ECS service. */}
      {offered('control-plane') && (
        <TabPanel id="control-plane" current={activeTab} idPrefix="audit">
          {controlPlaneOpened && ControlPlaneTab && <ControlPlaneTab />}
        </TabPanel>
      )}
    </Box>
  )
}
