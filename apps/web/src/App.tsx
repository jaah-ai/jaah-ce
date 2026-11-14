import { Suspense, useEffect, useState, type ComponentProps, type ComponentType } from 'react'
import { lazyRetry as lazy, prefetchChunksAfterLoad } from './lib/lazyRetry'
import { Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom'
import Box from '@mui/material/Box'
import CircularProgress from '@mui/material/CircularProgress'
import { TERMS_ACCEPT_PATH } from './api'
import { useAuth, useCan } from './auth'
import { ConsoleDockProvider, useConsoleDock } from './components/ConsoleDock'
import { LAUNCHER_CLEARANCE_SX } from './ui/launcherSeat'
import { SupportChatProvider } from './lib/helpSurface'
import OnboardingWidget, { ONBOARDING_BAR_VAR } from './components/onboarding/OnboardingWidget'
import HomeLanding from './components/onboarding/HomeLanding'
import { MoveTaskToast } from './components/projects/jaahRepoReplaced'
import { clearLoginReturn, loginReturn, rememberLoginReturn } from './lib/loginReturn'
import { AgentsLayout, CustomizeLayout, DirectoryLayout } from './components/RouteTabLayout'
import {
  ListDetailProvider,
  ListDetailShell,
  SPLIT_MAIN_SX,
  useListDetail,
  useListDetailRouting,
} from './components/ListDetail'
import ActAsBar from './components/ActAsBar'
import PendingOrgBanner from './components/PendingOrgBanner'
import UpdateBanner from './components/UpdateBanner'
import RequireFeature from './components/RequireFeature'
import RequireSuperadmin from './components/RequireSuperadmin'
import { RequirePlanSwitch } from './lib/planSwitches'
import { RequireTemplateEditor } from './pages/templates/access'
import Sidebar from './components/Sidebar'
import StaleTabDialog from './components/StaleTabDialog'
import OutageNotice from './components/OutageNotice'
import UnknownRoute from './components/UnknownRoute'
import { useOrgScopeUrl } from './hooks/useOrgScopeUrl'
import { useOrgScope } from './lib/orgScope'
import { OrgScopeBar } from './ui/OrgScopeControl'
import { usePhone } from './hooks/usePhone'
import { BOTTOM_NAV_HEIGHT, PHONE, SHELL_INSET } from './ui/listStyles'
import { SIZE, grid } from './theme'
// The persona vocabulary's one fetch (see that file for why it mounts on the shell).
import { usePersonaRegistry } from './components/tasks/usePersonaRegistry'
import { invitationReturn } from './lib/invitationReturn'
import { CLOUD } from './lib/cloudSurfaces'
import { isEnabled } from './lib/editionFeatures'
import { editionModule } from './lib/editionModule'
import { IS_CE } from './lib/edition'
import { notInEditionRoutes } from './routesNotInEdition'
import { loadTaskDetailPage } from './pages/loadTaskDetailPage'
import { taskPageFallback } from './pages/taskDetail/TaskPageSkeleton'

// Route-level code splitting: each page is its own chunk, fetched on first navigation.
const AuditPage = lazy(() => import('./pages/audit/AuditPage'))
const AuditEventPage = lazy(() => import('./pages/audit/AuditEventPage'))
const CommandDetailPage = lazy(() => import('./pages/audit/CommandDetailPage'))
const PaneLogPage = lazy(() => import('./pages/audit/PaneLogPage'))
const ApiKeyDetailPage = lazy(() => import('./pages/settings/apiKeys/ApiKeyDetailPage'))
/* One Gmail connection's page (`tenant_mail`): routed only where the edition has org email (`lib/editionModule.ts`). */
const loadGmailConnection = editionModule(isEnabled('tenant_mail'),
  () => import.meta.glob<{ default: ComponentType }>('./pages/settings/email/GmailConnectionPage.tsx'))
const GmailConnectionPage = loadGmailConnection ? lazy(loadGmailConnection) : null
/* Org email's two create pages, routed where the Gmail connection page is. */
const loadServiceAccountNew = editionModule(isEnabled('tenant_mail'),
  () => import.meta.glob<{ default: ComponentType }>('./pages/settings/email/ServiceAccountNewPage.tsx'))
const ServiceAccountNewPage = loadServiceAccountNew ? lazy(loadServiceAccountNew) : null
const loadNewAddress = editionModule(isEnabled('tenant_mail'),
  () => import.meta.glob<{ default: ComponentType }>('./pages/settings/email/NewAddressPage.tsx'))
const NewAddressPage = loadNewAddress ? lazy(loadNewAddress) : null
/* One model's rates: routed where Settings carries the Model pricing panel (`model_prices`, pages/Settings.tsx). */
const loadModelPrice = editionModule(isEnabled('model_prices'),
  () => import.meta.glob<{ default: ComponentType<{ mode?: 'create' }> }>('./pages/settings/modelPrices/ModelPriceEditPage.tsx'))
const ModelPriceEditPage = loadModelPrice ? lazy(loadModelPrice) : null
const TagGroupEditPage = lazy(() => import('./pages/settings/tags/TagGroupEditPage'))
const TagEditPage = lazy(() => import('./pages/settings/tags/TagEditPage'))
const PipelineTemplateDetailPage = lazy(() => import('./pages/pipelineTemplates/PipelineTemplateDetailPage'))
const Login = lazy(() => import('./pages/Login'))
const MfaVerify = lazy(() => import('./pages/MfaVerify'))
const MfaEnrol = lazy(() => import('./pages/MfaEnrol'))
const TermsAcceptPage = lazy(() => import('./pages/TermsAcceptPage'))
const CeSetupPage = lazy(() => import('./pages/ce/CeSetupPage'))
// Its own chunk: the charts library (`@mui/x-charts`) is downloaded by the operator only.
/* The operator dashboard (`operator_dashboard`) and the shell's plan-end notice (`billing`), loaded where present
   (`lib/editionModule.ts`). */
const loadDashboard = editionModule(isEnabled('operator_dashboard'),
  () => import.meta.glob<{ default: ComponentType }>('./pages/dashboard/OperatorDashboard.tsx'))
const OperatorDashboard = loadDashboard ? lazy(loadDashboard) : null
/* The command center (`exec_dashboard`): a tenant Owner's or Admin's Reports, cloud-only like the operator's. */
const loadCommandCenter = editionModule(isEnabled('exec_dashboard'),
  () => import.meta.glob<{ default: ComponentType }>('./pages/command-center/CommandCenter.tsx'))
const CommandCenter = loadCommandCenter ? lazy(loadCommandCenter) : null
/* A Jarvis view of it (`/command-center/view`): the same edition, its own chunk. */
const loadTempView = editionModule(isEnabled('exec_dashboard'),
  () => import.meta.glob<{ default: ComponentType }>('./pages/command-center/TempView.tsx'))
const TempView = loadTempView ? lazy(loadTempView) : null
const PlanEndedBanner = editionModule(isEnabled('billing'),
  () => import.meta.glob<ComponentType>('./components/PlanEndedBanner.tsx', { eager: true, import: 'default' }))
const Projects = lazy(() => import('./pages/Projects'))
const ProjectEditPage = lazy(() => import('./pages/ProjectEditPage'))
const ProjectCreateWizard = lazy(() => import('./pages/ProjectCreateWizard'))
const ProjectDocsPage = lazy(() => import('./pages/ProjectDocsPage'))
const ProjectEnvVarPage = lazy(() => import('./pages/projects/ProjectEnvVarPage'))
const PipelinePage = lazy(() => import('./pages/pipeline/PipelinePage'))
const TaskDetailPage = lazy(loadTaskDetailPage)
const FeatureEditPage = lazy(() => import('./pages/FeatureEditPage'))
const Credentials = lazy(() => import('./pages/credentials/Credentials'))
const AgentsPage = lazy(() => import('./pages/agents/AgentsPage'))
const CatalogPage = lazy(() => import('./pages/agents/CatalogPage'))
const RegistryPage = lazy(() => import('./pages/agents/RegistryPage'))
const AgentEditPage = lazy(() => import('./pages/agents/AgentEditPage'))
const Accounts = lazy(() => import('./pages/credentials/Accounts'))
const CredentialEditPage = lazy(() => import('./pages/credentials/CredentialEditPage'))
const CredentialNewRoute = lazy(() => import('./pages/credentials/CredentialNewRoute'))
const AccountEditPage = lazy(() => import('./pages/credentials/AccountEditPage'))
const CustomConnectorPage = lazy(() => import('./pages/credentials/custom/CustomConnectorPage'))
const Settings = lazy(() => import('./pages/Settings'))
const SettingsAreaPath = lazy(() => import('./pages/Settings').then(m => ({ default: m.SettingsAreaPath })))
const WorkflowDetailPage = lazy(() => import('./pages/workflows/WorkflowDetailPage'))
const WorkflowRunPage = lazy(() => import('./pages/workflows/WorkflowRunPage'))
const PersonaDetailPage = lazy(() => import('./pages/personas/PersonaDetailPage'))
const AutomationEditPage = lazy(() => import('./pages/automations/AutomationEditPage'))
const AutomationRunPage = lazy(() => import('./pages/automations/AutomationRunPage'))
const TrackerList = lazy(() => import('./pages/trackers/TrackerList'))
const TrackerDetailPage = lazy(() => import('./pages/trackers/TrackerDetailPage'))
const TrackerNewPage = lazy(() => import('./pages/trackers/TrackerNewPage'))
const WebhookList = lazy(() => import('./pages/webhooks/WebhookList'))
const WebhookDetailPage = lazy(() => import('./pages/webhooks/WebhookDetailPage'))
const NoAccess = lazy(() => import('./pages/NoAccess'))
const ProfilePage = lazy(() => import('./pages/ProfilePage'))
const NotificationsPage = lazy(() => import('./pages/NotificationsPage'))
const GitConnectDonePage = lazy(() => import('./pages/GitConnectDonePage'))
const GitAccountNewPage = lazy(() => import('./pages/GitAccountNewPage'))
const PlaybooksPage = lazy(() => import('./pages/playbooks/PlaybooksPage'))
const PlaybookEditPage = lazy(() => import('./pages/playbooks/PlaybookEditPage'))
const TemplatesManagePage = lazy(() => import('./pages/templates/TemplatesManagePage'))
const TemplateEditPage = lazy(() => import('./pages/templates/TemplateEditPage'))
const TemplatesPage = lazy(() => import('./pages/templates/TemplatesPage'))
const DeploymentDetailPage = lazy(() => import('./pages/production-qa/DeploymentDetailPage'))
const IncidentsPage = lazy(() => import('./pages/production-qa/IncidentsPage'))
const IncidentDetailPage = lazy(() => import('./pages/production-qa/IncidentDetailPage'))

function RequireAuth() {
  const { admin, loading } = useAuth()
  // The REAL location: behind an open editor the shell renders the list (`components/ListDetail.tsx`), so a cold
  // `/tasks/42` reads `/tasks` here; the editor's own address is `detailLocation`.
  const routed = useLocation()
  const location = useListDetail()?.detailLocation ?? routed
  if (loading) return <IdentityLoading />
  if (!admin) {
    // Sign-in ends on `/`, which returns here (`lib/loginReturn.ts`).
    rememberLoginReturn(`${location.pathname}${location.search}${location.hash}`)
    return <Navigate to="/login" replace />
  }
  // The Terms gate (`pages/TermsAcceptPage.tsx`): nothing of the console renders until they are accepted.
  if (admin.terms_pending) {
    rememberLoginReturn(`${location.pathname}${location.search}${location.hash}`)
    return <Navigate to={TERMS_ACCEPT_PATH} replace />
  }
  return <Outlet />
}

/* ── THE SHELL LAYOUT CONTRACT (single home) ──
   Route children size themselves against this box, so these values are a published contract.

     scrollport owner = `.app-main` (the <main> below). NOT the document, NOT `.app-layout`.

   `.app-layout`  →  sx `{ display: 'flex', height: '100dvh', overflow: 'hidden' }`
   `.app-main`    →  sx `{ flex: 1, overflow: 'auto', minWidth: 0, position: 'relative',
                            padding: SHELL_INSET (9px, `ui/listStyles.ts`; a 4px right inset beside a
                              split detail pane: ListDetail.tsx's SPLIT_MAIN_SX),
                            ...LAUNCHER_CLEARANCE_SX (above the phone, 88px under the content but on a
                              form-action-bar or `data-shell-fill` page; a form action bar keeps the
                              launcher's seat at its right end but beside an open detail pane),
                            '@media (max-width:480px)': { padding: '1rem',
                              paddingTop: '0.75rem',
                              paddingBottom: 'calc(1rem + 60px + var(--onboarding-bar-height, 0px))',
                              scrollPaddingBottom: 'calc(60px + var(--onboarding-bar-height, 0px))',
                              '&:has([data-form-action-bar])': { paddingBottom: 'calc(1rem + 60px)',
                                scrollPaddingBottom: '60px' } } }`

   The boxes carry `data-testid`s (`app-layout`/`app-main`); the class names above are identities for
   readers, not live selectors. */
const APP_LAYOUT_SX = { display: 'flex', height: '100dvh', overflow: 'hidden' } as const
const APP_MAIN_SX = {
  flex: 1,
  overflow: 'auto',
  minWidth: 0,
  // The containing block of every absolute descendant with no positioned ancestor (a visually hidden live
  // region, `ui/visuallyHidden.ts`): unpositioned, they escaped into the document's overflow, and a
  // `scrollIntoView` then scrolled the document too, carrying the shell and its sticky bars off screen.
  position: 'relative',
  padding: SHELL_INSET,
  ...LAUNCHER_CLEARANCE_SX,
  // The console's phone breakpoint is 480px, not MUI's `sm`. The bottom inset is the Sidebar's fixed
  // bottom bar, plus the onboarding phone bar above it while that is drawn (`ONBOARDING_BAR_VAR`, set by
  // components/onboarding/OnboardingWidget.tsx; 0 otherwise). Longhands: the 1rem HORIZONTAL inset is
  // load-bearing (`.table-wrap`'s -1rem full-bleed cancels exactly that). The scroll padding is the same
  // overlay, so a control focused or scrolled to (Save profile) stops above the bars, never behind them.
  [PHONE]: {
    padding: '1rem',
    paddingTop: '0.75rem',
    paddingBottom: `calc(1rem + ${BOTTOM_NAV_HEIGHT}px + var(${ONBOARDING_BAR_VAR}, 0px))`,
    // The onboarding bar stands down while a form action bar is DRAWN and then clears the variable itself
    // (OnboardingWidget.tsx), so its room goes with it: no `:has` here, which also matched a form mounted in a
    // hidden tab panel and left the drawn bar over the Timeline's last rows.
    scrollPaddingBottom: `calc(${BOTTOM_NAV_HEIGHT}px + var(${ONBOARDING_BAR_VAR}, 0px))`,
  },
} as const

/** Back to the deep link a sign-in interrupted (`lib/loginReturn.ts`), forgetting it as the move happens. */
function LoginReturnTo({ to }: { to: string }) {
  useEffect(() => clearLoginReturn(), [])
  return <Navigate to={to} replace />
}

/** `/`: everyone's `HomeLanding` (the Tasks board). First, a sign-in that began at an invitation link returns to that
 *  invitation (`lib/invitationReturn.ts`), and one that interrupted any other deep link returns to it
 *  (`lib/loginReturn.ts`): every sign-in door lands here. */
function HomeRoute() {
  /* Read once per mount: `LoginReturnTo` forgets it as it moves, and a re-render before the move lands must not
     fall through to `HomeLanding`'s own redirect and race it. */
  const [back] = useState(loginReturn)
  const invitation = invitationReturn()
  if (invitation) return <Navigate to={invitation} replace />
  if (back !== null) return <LoginReturnTo to={back} />
  return <HomeLanding />
}

/** `/reports`: the operator's dashboard (`pages/dashboard/`, operator-only by its API's authz), or a tenant Owner's or
 *  Admin's command center (`pages/command-center/`, `@org_admin`). Twin of the Sidebar's Reports row. The operator
 *  plane is the cloud's: the community edition has no operator dashboard. */
function ReportsRoute() {
  const { admin } = useAuth()
  if (admin?.is_operator === true && CLOUD && OperatorDashboard) return <OperatorDashboard />
  return CommandCenter ? <CommandCenter /> : <UnknownRoute />
}

/** `/command-center`, the command center's former address: Jarvis answers and bookmarks still link it, query kept. */
function CommandCenterRedirect() {
  const { search } = useLocation()
  return <Navigate to={`/reports${search}`} replace />
}

/** `/settings/api-keys`: the API keys list this viewer can open — Settings' section with `system:view`, else
 *  Profile › Security's. Holds, as `RequireFeature`, while the identity is unresolved. */
function ApiKeysListRoute() {
  const { loading, resolving } = useAuth()
  const can = useCan()
  if (loading) return null
  if (can('system:view')) return <Navigate to="/settings?area=api-keys&section=api-keys" replace />
  if (resolving) return null
  return <Navigate to="/profile?tab=security" replace />
}

/** The shell's route-transition fallback. A stock `<CircularProgress>`, not `ui/LoadingState`, so the
 *  shell and the shared primitives stay independent. */
function ShellFallback() {
  return (
    <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 5 }}>
      <CircularProgress data-testid="loading-state" size={SIZE.iconLg} aria-label="Loading" />
    </Box>
  )
}

/** `RequireAuth` while the identity read is out (a first visit or after sign-out: no cached identity): the shell's
 *  spinner. Nothing of the console renders behind it, so no guard decides on an unknown identity. */
function IdentityLoading() {
  return (
    <Box data-testid="identity-loading" sx={{ padding: SHELL_INSET, paddingBottom: grid(4.5), [PHONE]: { padding: '1rem' } }}>
      <ShellFallback />
    </Box>
  )
}

/* ── THE SHELL'S ROUTE TABLE ── every page behind sign-in. A constant because it renders TWICE: once by
   `App`'s `<Routes>` (the app pane) and once into the list/detail pane (`DetailRoutes`,
   components/ListDetail.tsx), so an editor opened beside its list passes the same guards it does as a page.
   A FUNCTION, not a shared element: fresh elements per render, so a re-render of `App` reaches every guard
   (a shared element object would let React bail out of re-rendering them). */
const shellRoutes = () => (
  <>
    {/* ── EVERY route below carries an explicit disposition ──
        The twin of the Sidebar's `requires` mapping: a destination hidden from the rail is
        unreachable by URL. EXHAUSTIVE: every path is inside a `RequireFeature`/`RequireSuperadmin`
        block or is one of six labelled exceptions:
          `/`                   — a redirect (`HomeLanding`); the TARGET's guard applies.
          `/no-access`          — the guard chain's terminal; guarding it would loop.
          `/profile`            — the self-service carve-out; must reach a zero-grant user
                                  (RequireFeature.tsx documents this shape).
          `/credentials` (+ its editors) — every user reads their own credentials;
                                  the API scopes the rows.
          `/compliance/verify`  — the cloud's (`routesCloud.tsx`): any signed-in user checks a downloaded
                                  manifest; the API asks for a session and nothing more.
          `*`                   — "Page not found" with a link to `/` (`UnknownRoute`).
        PRESENTATION ONLY, NEVER THE SECURITY BOUNDARY (components/RequireFeature.tsx). */}

    {/* Tasks is the landing surface, and THIS ROW is the single home for that: `/` (root, stale
        bookmarks, Login, RequireSuperadmin, the `*` catch-all's Go home) routes through here. One detour: a
        sign-in that interrupted a deep link returns to it. Otherwise a redirect
        (`components/onboarding/HomeLanding.tsx`); the TARGET's guard applies. */}
    <Route path="/" element={<HomeRoute />} />
    {/* Reports (`ReportsRoute`): the operator's dashboard or a tenant Owner's or Admin's command center, as their
        APIs are operator-only and `@org_admin` (`isOrgAdminSeat` admits the operator). Absent where the edition
        leaves both out. */}
    {(CommandCenter || (CLOUD && OperatorDashboard)) && (
      <Route element={<RequireSuperadmin orgAdmin />}>
        <Route path="/reports" element={<ReportsRoute />} />
        {CommandCenter && <Route path="/command-center" element={<CommandCenterRedirect />} />}
        {TempView && <Route path="/command-center/view" element={<TempView />} />}
      </Route>
    )}

    {/* ── DIRECTORY: ONE STRIP OVER ITS LIST PAGES ──
        `components/RouteTabLayout.tsx` draws the strip; `lib/directoryTabs.ts` is the single table of
        tabs and gates, each the twin of the guard wrapping its page here. Only the LIST pages sit
        under the strip — editors (`/projects/:id`, `/nodes/:nodeId`, …) keep their own routes below.
        Each page keeps its original address, so no link breaks. */}
    <Route element={<DirectoryLayout />}>
      <Route element={<RequireFeature permission="projects:view" />}>
        <Route path="/projects" element={<Projects />} />
      </Route>
      {/* People and Machines' node roster: the cloud's (`routesCloud.tsx`). */}
      {CLOUD?.cloudRoutes.directory()}
      {/* Secrets (`pages/credentials/Credentials.tsx`), UNGATED: every signed-in user reads it, and
          `authz_guard` scopes a keyless session to the rows it CREATED AND IS STILL GRANTED
          (`authz.owned_credential_clause`). */}
      <Route path="/credentials" element={<Credentials />} />
      {/* AI Accounts (`pages/credentials/Accounts.tsx`, the runner account roster) keeps `/accounts` under
          `accounts:view`, the key `routes/accounts.py` declares, whatever the plan. */}
      <Route element={<RequireFeature permission="accounts:view" />}>
        <Route path="/accounts" element={<Accounts />} />
      </Route>
    </Route>

    {/* ── CUSTOMIZE: ONE STRIP OVER PLAYBOOKS, TEMPLATES, AGENTS AND COMPLIANCE PACKS ──
        `components/RouteTabLayout.tsx` draws the strip; `lib/customizeTabs.ts` is its table of tabs and gates, each
        the twin of the guard here. Nav half: components/Sidebar.tsx's Customize row. */}
    <Route element={<CustomizeLayout />}>
      {/* Playbooks: no feature key — every org member reads, and the API 403s a non-admin's mutation
          (`routes/playbooks.py`) — but only while the org's plan has Playbooks on, or for the operator. */}
      <Route element={<RequirePlanSwitch name="playbooks_enabled" operator />}>
        <Route path="/playbooks" element={<PlaybooksPage />} />
      </Route>
      {/* The template gallery: every member browses, stars and (with `tasks:create`) starts — the API answers for the
          caller's own org (`routes/task_templates.py`), so no key guards the address. */}
      <Route path="/templates" element={<TemplatesPage />} />
      {/* Agents' sub-tabs under their own strip (`pages/agents/agentsTabs.tsx`), nested under Customize's.
          `agents:view`, the key `routes/agents.py` declares. Catalog, the agent marketplace, opens whatever the plan;
          the others only while the org's provider grid has Agents on. */}
      <Route element={<AgentsLayout />}>
        <Route element={<RequireFeature permission="agents:view" />}>
          <Route path="/agents/catalog" element={<CatalogPage />} />
          <Route element={<RequirePlanSwitch name="agents" />}>
            <Route path="/agents" element={<AgentsPage />} />
            <Route element={<RequirePlanSwitch name="acp_registry" />}>
              <Route path="/agents/registry" element={<RegistryPage />} />
            </Route>
          </Route>
        </Route>
      </Route>
      {/* Compliance Packs: the cloud's (`routesCloud.tsx`). */}
      {CLOUD?.cloudRoutes.customize()}
    </Route>

    {/* The guard chain's terminal destination. Unguarded on purpose — see RequireFeature. */}
    <Route path="/no-access" element={<NoAccess />} />

    {/* The self-service carve-out (see the disposition comment above). Unguarded. */}
    <Route path="/profile" element={<ProfilePage />} />
    {/* Profile > Git's Add form as its own page, for a form's nested create (`hooks/useNestedCreate.ts`). Unguarded, as Profile. */}
    <Route path="/profile/git/new" element={<GitAccountNewPage />} />
    {/* API keys: every member issues and revokes their own; Org Owners/Admins see and revoke all (`routes/api_keys.py`).
        Unguarded, as Profile: a member without `system:view` cannot open Settings. The list is
        `pages/settings/apiKeys/ApiKeysSection.tsx`, in Settings or (without `system:view`) Profile › Security;
        `/settings/api-keys` (the breadcrumb's and the bell's link) goes to whichever this viewer has. */}
    <Route path="/settings/api-keys" element={<ApiKeysListRoute />} />
    <Route path="/settings/api-keys/new" element={<ApiKeyDetailPage mode="create" />} />
    <Route path="/settings/api-keys/:keyId" element={<ApiKeyDetailPage />} />
    {/* The caller's own notifications (the bell's View all): every signed-in user has a bell. Unguarded, as Profile. */}
    <Route path="/notifications" element={<NotificationsPage />} />
    {/* The create wizard's GitHub popup lands here and closes (`githubOAuthPopup.ts`). Unguarded, as Profile. */}
    <Route path="/git-connect/done" element={<GitConnectDonePage />} />

    {/* Task templates' manage list and editor (`pages/templates/`): the operator's global library, and an org
        template editor's own (`templates:edit` while the org's "Templates (org)" switch is on) — `RequireTemplateEditor`,
        the twin of `routes/task_templates.py`'s write gate. Static before `:id`: `manage` and `new` are no template id. */}
    <Route element={<RequireTemplateEditor />}>
      <Route path="/templates/manage" element={<TemplatesManagePage />} />
      <Route path="/templates/new" element={<TemplateEditPage key="new" mode="create" />} />
      <Route path="/templates/:id" element={<TemplateEditPage />} />
    </Route>
    {/* Static before `:id`: `new` is not a playbook id. The list's plan guard. */}
    <Route element={<RequirePlanSwitch name="playbooks_enabled" to="/playbooks" operator />}>
      <Route path="/playbooks/new" element={<PlaybookEditPage key="new" mode="create" />} />
      <Route path="/playbooks/:id" element={<PlaybookEditPage />} />
    </Route>

    {/* One custom agent's page (`pages/agents/AgentEditPage.tsx`), beside the Custom agents list (`lib/listDetail.ts`), under
        the list's own guards. Static before `:id`: `new`, `catalog` and `registry` are no agent id. */}
    <Route element={<RequireFeature permission="agents:view" />}>
      <Route element={<RequirePlanSwitch name="agents" />}>
        <Route path="/agents/new" element={<AgentEditPage key="new" mode="create" />} />
        <Route path="/agents/:id" element={<AgentEditPage />} />
      </Route>
    </Route>

    {/* The project EDIT page takes the list's key (writes are gated server-side). The CREATE page is
        keyed on `projects:create`, the key `POST /api/projects` asks, so a deep link does not end
        in a bare 403 on Save. */}
    <Route element={<RequireFeature permission="projects:view" />}>
      <Route element={<RequireFeature permission="projects:create" />}>
        <Route path="/projects/new" element={<ProjectCreateWizard />} />
      </Route>
      <Route path="/projects/:id" element={<ProjectEditPage />} />
      {/* The docs reader is its own route: a deep-linkable READING surface, not a setting. */}
      <Route path="/projects/:id/docs" element={<ProjectDocsPage />} />
      {/* One environment variable, beside its project's Environment tab; `new` is the same page in create mode. */}
      <Route path="/projects/:id/env/new" element={<ProjectEnvVarPage mode="create" />} />
      <Route path="/projects/:id/env/:varId" element={<ProjectEnvVarPage />} />
    </Route>

    {/* A deploy run's page and Production QA's incidents (`pages/production-qa/`): `projects:view`, the key
        `routes/production_qa.py` declares; each read is limited to the projects the caller may view, while the org's plan
        has Production QA on, or for the operator. Releases are the pipeline page's (`/tasks`); the retired org list's
        address lands there. */}
    <Route element={<RequireFeature permission="projects:view" />}>
      <Route path="/deployments" element={<Navigate to="/tasks" replace />} />
      <Route element={<RequirePlanSwitch name="production_qa" operator />}>
        <Route path="/deployments/:id" element={<DeploymentDetailPage />} />
        <Route path="/incidents" element={<IncidentsPage />} />
        <Route path="/incidents/:id" element={<IncidentDetailPage />} />
      </Route>
    </Route>

    {/* ── THE ORG-ADMIN TIER'S RE-KEY ──
        Nodes, Audit, Sessions and the account roster each ask their route's own key (`nodes:*`,
        `audit:view`, `sessions:*`, `accounts:*`). ONE KEY EACH, NOT A DISJUNCTION WITH
        `system:view`: `system:*` no longer reaches these routes, so naming it would offer a row the
        API refuses. `system` keeps the SETTINGS subject.
        `/nodes/:nodeId`, `/nodes/groups/:groupId` and `/nodes/hosts/:hostId` take `/nodes`' key: a detail view is the collection's
        `view`. */}
    <Route element={<RequireFeature anyOf={['audit:view', 'sessions:view', 'tasks:create', 'projects:view']} />}>
      {/* ── AUDIT: ONE TAB SHELL, TWELVE TABS ──
          `pages/audit/AuditPage.tsx`: the audit trail (`/audit`), Nodes (`/audit/nodes`, with Slot
          Intents at `/audit/nodes/slot-intents`), Sessions (`/sessions`, Efficiency at
          `/sessions/efficiency`), Workflows (`/audit/workflows`, with Schedule and Catalogue leaves),
          Personas (`/audit/personas`), Exclusions (`/audit/exclusions`), Control plane
          (`/audit/control-plane`), Client errors (`/audit/client-errors`), Automations (`/automations`) and Slots
          (`/worktrees`), Merge lanes (`/audit/merge-lanes`) and Memory (`/audit/memory`), the last four routed below. The nav half is
          components/Sidebar.tsx's Audit row.
          TAB URLS ARE PATHS, NOT `?tab=`, so existing bookmarks and links resolve unchanged.
          THE GUARD ASKS EITHER KEY (`anyOf`, `projects:view` for Merge lanes and Memory): the shell is one destination, so its admitting key
          cannot be narrower than the union its tabs serve (the rail's Audit row offers that union).
          It discloses nothing: `GET /api/audit` still asks `audit:view`, and the shell resolves a
          reader to the first tab they are offered (`activeTab`, its leaf-correction latch). `/audit` is
          that entry, so it takes no plan guard: with Activity off the plan its tab stays and says so (`NotOnPlan`). */}
      <Route path="/audit" element={<AuditPage />} />
      {/* One activity row, opened from the feed into the list/detail pane (`lib/listDetail.ts`), under Activity's
          plan switch. */}
      <Route element={<RequirePlanSwitch name="audit_enabled" operator />}>
        <Route path="/audit/events/:eventId" element={<AuditEventPage />} />
      </Route>
    </Route>
    {/* Audit's operator tabs sit OUTSIDE the shell's `anyOf` gate, the operator gate outermost: a refused reader
        reads "Page not found", never the feature gate's "No access" that would mark the address. */}
    {/* Personas asks `tasks:create` (`routes/personas.py`); the console offers it to the operator alone. */}
    <Route element={<RequireSuperadmin />}>
      <Route element={<RequireFeature permission="tasks:create" />}>
        <Route path="/audit/personas" element={<AuditPage />} />
      </Route>
    </Route>
    {/* Nodes, Workflows, the workflow catalogue, Exclusions, Control plane and Client errors are the operator's. Nodes' two
        sub-tabs ask DIFFERENT keys (the command log `system:view`, `GET /api/nodes/intents` `nodes:view`),
        filtered per leaf (`pages/audit/AuditPage.tsx`'s `visibleNodeTabs`). Workflows' Runs and catalogue
        (`routes/workflows.py`) are `@superadmin_only(Tenancy.OPERATOR)`; `tenancy_exclusions` is flat —
        `routes/tenancy_exclusions.py`; `GET /api/control-plane/ecs` is `require_operator` — `routes/cp_ecs.py`; `GET /api/client-events` is `superadmin_only`. */}
    <Route element={<RequireSuperadmin />}>
      <Route path="/audit/nodes" element={<AuditPage />} />
      <Route path="/audit/nodes/slot-intents" element={<AuditPage />} />
      {/* One command, opened from the log into the list/detail pane (`lib/listDetail.ts`). */}
      <Route path="/audit/nodes/commands/:commandId" element={<CommandDetailPage />} />
      <Route path="/audit/workflows" element={<AuditPage />} />
      <Route path="/audit/workflows/schedule" element={<AuditPage />} />
      <Route path="/audit/workflows/catalogue" element={<AuditPage />} />
      <Route path="/audit/exclusions" element={<AuditPage />} />
      <Route path="/audit/control-plane" element={<AuditPage />} />
      <Route path="/audit/client-errors" element={<AuditPage />} />
    </Route>

    {/* Audit > Automations — the org-wide list of schedules and event triggers (`pages/automations/AutomationList.tsx`;
        each project's and machine's page also lists its own). Every `/api/automations` route is `@org_admin`
        (`routes/automations.py`), scoped per org. */}
    <Route element={<RequireSuperadmin orgAdmin />}>
      <Route path="/automations" element={<AuditPage />} />
    </Route>
    {/* Audit > Slots (`pages/Worktrees.tsx`): `worktrees:view`, the key `/api/worktrees` declares, and the operator's
        alone. The node roster is a different key (`nodes:view`), so a worktrees-only principal sees short node ids. */}
    <Route element={<RequireSuperadmin />}>
      <Route element={<RequireFeature permission="worktrees:view" />}>
        <Route path="/worktrees" element={<AuditPage />} />
      </Route>
    </Route>

    {/* Audit > Merge lanes and Audit > Memory (`pages/audit/MergeLanesTab.tsx`, `MemoryTab.tsx`): every project the
        reader may view, `?project=<id>` narrowing to one. Their reads ask `projects:view` (`routes/merge_lane_rows.py`,
        `routes/project_memory.py`), each project's rows filtered by the reader's own scopes. */}
    <Route element={<RequireFeature permission="projects:view" />}>
      <Route path="/audit/merge-lanes" element={<AuditPage />} />
      <Route path="/audit/memory" element={<AuditPage />} />
    </Route>

    {/* AUDIT > SESSIONS at two more addresses, under `routes/sessions.py`'s own key. `sessions:edit`
        (transcript DOWNLOAD) is asked at the control, so a reader still sees which sessions lack
        a transcript. And the Session transcripts plan switch: off, a tenant reaches none of them. */}
    <Route element={<RequireFeature permission="sessions:view" />}>
      <Route element={<RequirePlanSwitch name="session_transcripts_enabled" operator />}>
        <Route path="/sessions" element={<AuditPage />} />
        <Route path="/sessions/efficiency" element={<AuditPage />} />
        {/* A dead run's pane tail; its GET asks `sessions:edit`, so a reader gets the page's 403 state. */}
        <Route path="/sessions/:sessionId/pane-log" element={<PaneLogPage />} />
      </Route>
    </Route>

    {/* AGENTS > AGENTS' editors (the runner accounts), keyed `accounts:view` as `routes/accounts.py` declares, the key the
        `/accounts` list route asks too. */}
    <Route element={<RequireFeature permission="accounts:view" />}>
      {/* The account form (New / Edit / Login) under the same key; each write keeps its own gate server-side
          and the page draws a seat without it read-only or "not allowed" (`AccountEditPage.tsx`). */}
      <Route path="/accounts/new" element={<AccountEditPage />} />
      <Route path="/accounts/login/:slug" element={<AccountEditPage />} />
      <Route path="/accounts/:id" element={<AccountEditPage />} />
    </Route>

    {/* ── SETTINGS: READABLE BY `system:view`, SAVEABLE BY AN OPERATOR ──
        Settings are per-org and `GET /api/settings` answers per org; `pages/Settings.tsx` renders
        read-only for a non-operator, so no save is left to 403.
        THE KEY IS WHAT `GET` ENFORCES: `routes/settings.py::get_settings` is
        `@feature("system", "view")`.
        THE WRITE IS STILL OPERATOR-ONLY, AND ITS SECOND CONJUNCT IS NOT A DEPENDENCY:
        `put_settings` carries `@feature("system", "edit")`, resolves `Depends(require_user)` and
        refuses IN-HANDLER on `tenancy/ids.py::is_operator_user` (`is_superadmin` AND an
        operator-plane org, `tenancy/ids.py::is_operator_plane`) — so a tenant's own superadmin
        is refused too. It is NOT the `require_superadmin` flag check, which a tenant superadmin
        passes; documenting that weaker gate invites "restoring" it. The API is the
        boundary. components/Sidebar.tsx's Settings row asks this key. */}
    <Route element={<RequireFeature permission="system:view" />}>
      <Route path="/settings" element={<Settings />} />
      {/* One Gmail connection: Settings › Organizations › Email's detail. Every door is the server's
          `@org_admin` (`routes/org_email.py`); the list is `pages/settings/email/EmailSection.tsx`. */}
      {ServiceAccountNewPage && <Route path="/settings/email/service-accounts/new" element={<ServiceAccountNewPage />} />}
      {NewAddressPage && <Route path="/settings/email/addresses/new" element={<NewAddressPage />} />}
      {GmailConnectionPage && <Route path="/settings/email/:connectionId" element={<GmailConnectionPage />} />}
      {/* Settings records, each beside its Settings section (lib/listDetail.ts): a tag group and its tags,
          a model's rates. Writes stay the server's (`put_settings`, the tag and
          model-price routes), whatever this page shows. */}
      <Route path="/settings/tags/new" element={<TagGroupEditPage mode="create" />} />
      <Route path="/settings/tags/:groupId" element={<TagGroupEditPage />} />
      <Route path="/settings/tags/:groupId/tags/new" element={<TagEditPage mode="create" />} />
      <Route path="/settings/tags/:groupId/tags/:tagId" element={<TagEditPage />} />
      <Route element={<RequireSuperadmin orgAdmin />}>
        {/* Pipeline templates are an Org Owner/Admin's, as the panel is (pages/Settings.tsx `pipelineTemplates`). */}
        <Route path="/settings/pipeline-templates/new" element={<PipelineTemplateDetailPage mode="create" key="new" />} />
        <Route path="/settings/pipeline-templates/:id" element={<PipelineTemplateDetailPage />} />
      </Route>
      {ModelPriceEditPage && <Route path="/settings/pricing/new" element={<ModelPriceEditPage mode="create" />} />}
      {ModelPriceEditPage && <Route path="/settings/pricing/:model" element={<ModelPriceEditPage />} />}
      <Route path="/settings/:area" element={<SettingsAreaPath />} />
    </Route>

    {/* A1b/CONTRACT-5: the New-task surface is the SAME page shape as `/tasks/:id`. react-router v6
        ranks a static segment above a dynamic one, so `new` is never read as an id.
        Keyed `tasks:create` (the one non-`:view` key here); the board's own create affordances ask
        the same key (`hooks/useTaskAccess.ts`, `useCanCreateTasks`). This is the URL backstop. */}
    <Route element={<RequireFeature permission="tasks:create" />}>
      <Route path="/tasks/new" element={<TaskDetailPage mode="create" />} />
      {/* THE CHAT create surface — the same form with the `advisor` preset.
          ITS KEY IS THE COARSER HALF OF WHAT THE SERVER ASKS: `POST /api/tasks` for an advisor also
          demands `worktrees:edit` ON THE TARGET PROJECT (`apps/api/app/routes/tasks/`,
          `create_task`), which a URL guard cannot evaluate before a project is picked. The buttons
          leading here ask both (`hooks/useTaskAccess.ts`, `useCanCreateChats`). A `tasks:create`-only
          principal typing this URL is refused on Save — intended; fix by granting, never by
          widening this route. */}
      <Route path="/tasks/new-chat" element={<TaskDetailPage mode="create-chat" />} />
    </Route>

    <Route element={<RequireFeature permission="tasks:view" />}>
      <Route path="/tasks" element={<PipelinePage />} />
      <Route path="/tasks/:id" element={<TaskDetailPage />} />
      {/* The Feature Edit page: viewing needs `tasks:view`; Save is gated by the row's `can_edit`. `/new` precedes
          `/:id`; it offers only the projects granting `tasks:create`, which `POST /api/features` re-checks. */}
      <Route path="/features/new" element={<FeatureEditPage mode="create" />} />
      <Route path="/features/:id" element={<FeatureEditPage />} />
    </Route>

    {/* THE EDITOR IS UNGATED, as the roster (`/credentials` above) that opens it for every
        authenticated user. `/new` precedes `/:id`.
        PRESENTATION ONLY (components/RequireFeature.tsx): `authz_guard` decides on every
        /api/credentials route, fail-closed, admitting a keyless session only to rows it CREATED
        AND IS STILL GRANTED (`authz.owned_credential_clause`); every other row 404s.
        It does NOT open `/accounts` or widen a WRITE (`CredentialEditPage` hides Test without
        `credentials:edit`). A bare `/new` is the integrations gallery (`CredentialNewRoute`). */}
    {/* Custom connectors (`pages/credentials/custom/`), ungated as the roster: every member reads; the API's
        `@org_admin` decides a definition's writes. Before `/credentials/:id`. The org's connectors are listed as
        cards in the New secret gallery's Custom category, where an old link to the list lands. */}
    <Route path="/credentials/custom" element={<Navigate to="/credentials/new?category=custom" replace />} />
    <Route path="/credentials/custom/new" element={<CustomConnectorPage mode="create" />} />
    <Route path="/credentials/custom/:id" element={<CustomConnectorPage />} />
    <Route path="/credentials/new" element={<CredentialNewRoute />} />
    <Route path="/credentials/:id" element={<CredentialEditPage />} />

    <Route element={<RequireSuperadmin />}>
      {/* ONE PERSONA, from the CP's dispatch tables — at Personas' own key and seat (the operator), reached from
          Audit's Personas and Workflows > Catalogue tabs. Two segments deep, so no collision with `/workflows/:name`. */}
      <Route element={<RequireFeature permission="tasks:create" />}>
        <Route path="/workflows/personas/:persona" element={<PersonaDetailPage />} />
      </Route>
    </Route>
    <Route element={<RequireSuperadmin />}>
      {/* ONE WORKFLOW, by registry NAME; an unknown name renders a named not-found. */}
      {/* ONE RUN, in the pane beside the Audit > Workflows runs ledger (`WorkflowRunPage.tsx`). `runs` is reserved:
          no workflow may be named it, and this two-segment path never reaches `/workflows/:name`. */}
      <Route path="/workflows/runs/:runId" element={<WorkflowRunPage />} />
      <Route path="/workflows/:name" element={<WorkflowDetailPage />} />
    </Route>

    <Route element={<RequireSuperadmin orgAdmin />}>
      {/* The automation editor (its list, `/automations`, is Audit's tab), every org admin's as
          the list is (this group's guard). `new` before `:id`. */}
      <Route path="/automations/new" element={<AutomationEditPage mode="create" key="new" />} />
      <Route path="/automations/:id" element={<AutomationEditPage />} />
      {/* One run, in the pane beside its automation's list (`AutomationRunPage.tsx`). */}
      <Route path="/automations/:id/runs/:runId" element={<AutomationRunPage />} />
      {/* The retired Triggers and Schedule addresses, still in delivered emails and Slack cards: the Automations
          list, never a 404. The API emits only the present addresses (`automation_alerts.AUTOMATIONS_PATH`). */}
      <Route path="/triggers" element={<Navigate to="/automations" replace />} />
      <Route path="/triggers/:id" element={<Navigate to="/automations" replace />} />
      <Route path="/crons/:id" element={<Navigate to="/automations" replace />} />
      {/* Issue trackers (Linear, Jira, Backlog): every `/api/tracker-connections` route is `@org_admin`
          (`routes/trackers.py`), as this group's guard. */}
      <Route path="/integrations/trackers" element={<TrackerList />} />
      <Route path="/integrations/trackers/new" element={<TrackerNewPage />} />
      <Route path="/integrations/trackers/:id" element={<TrackerDetailPage />} />
      {/* Outgoing webhooks: every `/api/webhooks` route is `@org_admin` (`routes/webhooks.py`), as this group's
          guard, and refused off the `webhooks_enabled` plan switch. `new` before `:id`. */}
      <Route element={<RequirePlanSwitch name="webhooks_enabled" />}>
        <Route path="/integrations/webhooks" element={<WebhookList />} />
        <Route path="/integrations/webhooks/new" element={<WebhookDetailPage mode="create" key="new" />} />
        <Route path="/integrations/webhooks/:id" element={<WebhookDetailPage />} />
      </Route>
    </Route>

    {/* The cloud console's own pages (`routesCloud.tsx`): billing, support, nodes, people, customers, products,
        compliance. */}
    {CLOUD?.cloudRoutes.shell()}
  </>
)

/** `<main>` beside a docked console runs near its divider, as beside an open editor (`SPLIT_MAIN_SX`). */
const APP_MAIN_DOCKED_SX = { ...APP_MAIN_SX, ...SPLIT_MAIN_SX }

/** The shell's workspace, inside `ConsoleDockProvider` so `<main>` can read whether the console is docked. */
function AppWorkspace(props: Omit<ComponentProps<typeof ListDetailShell>, 'mainSx'>) {
  const { docked } = useConsoleDock()
  return <ListDetailShell {...props} mainSx={docked ? APP_MAIN_DOCKED_SX : APP_MAIN_SX} />
}

function AuthLayout() {
  // The page's error boundary lives INSIDE the layout (`ListDetailShell`) so the Sidebar survives a page crash.
  /* THE PERSONA VOCABULARY, fetched once for the authenticated shell (`personaRegistry.ts` is a module
     cache read synchronously). Here because `AuthLayout` stays mounted across every navigation: one
     fetch per page load, one re-render for every consumer. The value is deliberately unused. */
  usePersonaRegistry()
  /* Every route chunk warmed once the signed-in shell is idle (`lib/lazyRetry.ts::prefetchChunks`), so navigation
     on a network that drops connections never waits on one. */
  useEffect(() => prefetchChunksAfterLoad(), [])
  /* The operator's org filter: `?orgs=` and the remembered scope kept in step, the page held only while a URL's
     orgs are still being resolved (`hooks/useOrgScopeUrl.ts`). */
  const scopeReady = useOrgScopeUrl()
  /* Part of both panes' keys: a scope change remounts the page, so a page that fetches in an effect (not
     through react-query, which `setOrgScope` resets) reads again under the new scope. */
  const scopeKey = useOrgScope()?.map(org => org.id).join(',') ?? ''
  const phone = usePhone()
  const listDetail = useListDetail()
  const detailPath = listDetail?.detailLocation?.pathname
  return (
    <Box data-testid="app-layout" sx={APP_LAYOUT_SX}>
      {/* THE WEB CONSOLE LIVES HERE, NOT IN A PAGE: `AuthLayout` stays mounted across navigation, so the
          terminal and its socket survive it. The provider wraps the row's CONTENTS because its host
          renders as a sibling after `children` (docked, an in-flow flex child beside `<main>`). The rest
          is single-homed in components/ConsoleDock.tsx. */}
      <ConsoleDockProvider>
        {/* The Help panel (components/support/SupportChat.tsx) is here for the same reason: its chat and
            a reply still streaming survive navigation. The Sidebar's Help button and the floating launcher open it. */}
        <SupportChatProvider>
          {/* The onboarding checklist (components/onboarding/OnboardingWidget.tsx), here for the same reason:
              a step's link navigates, and the checklist must still be up on the page it lands on. On desktop it
              docks in flow at the row's right end (its own `order`), so it narrows the page rather than covering it. */}
          <OnboardingWidget />
          {/* A tab an org switch in another tab left behind: blocks until reloaded (`lib/activeOrg.ts`). */}
          <StaleTabDialog />
          {/* A project Save that moved its code: the task's link, on whatever page the Save left for. */}
          <MoveTaskToast />
          <Sidebar />
          {/* THE LIST/DETAIL SPLIT (components/ListDetail.tsx): while an editor is open beside its list,
              `<main>` renders the page it was opened from and the pane renders the editor — three resizable
              panes with the console. `<main>` stays the workspace's first child either way, so that page
              never remounts. */}
          <AppWorkspace
            routes={shellRoutes()}
            page={<Outlet />}
            /* A task, full page or beside the board: the task page's own skeleton while its
               chunk loads, not the spinner. */
            fallback={taskPageFallback(listDetail?.mainLocation.pathname) ?? <ShellFallback />}
            detailFallback={taskPageFallback(detailPath) ?? <ShellFallback />}
            ready={scopeReady}
            scopeKey={scopeKey}
            mainTop={(
              /* Above every page, in flow (never an overlay): the org this tab acts as, then a new build's Reload,
                 a pending org's or an ending plan's notice. A nameless account's ask is a quiet mark on the profile
                 menu (`components/Sidebar.tsx`), never standing prose here. */
              <>
                <ActAsBar />
                <UpdateBanner />
                <PendingOrgBanner />
                {PlanEndedBanner && <PlanEndedBanner />}
                {phone && <OrgScopeBar />}
              </>
            )}
          />
        </SupportChatProvider>
      </ConsoleDockProvider>
    </Box>
  )
}

export default function App() {
  /* ONE URL, TWO LOCATIONS: an editor's URL renders its list here and itself in the pane
     (components/ListDetail.tsx). Without a split, `mainLocation` is the URL. */
  const listDetail = useListDetailRouting()
  return (
    <ListDetailProvider value={listDetail}>
    {/* Over every route, sign-in included: the API cannot be reached (`components/OutageNotice.tsx`). */}
    <OutageNotice />
    <Suspense fallback={<ShellFallback />}>
      <Routes location={listDetail.mainLocation}>
        <Route path="/login" element={<Login />} />
        {/* The second factor: public, authenticated by the pending-MFA cookie a first factor set. */}
        <Route path="/login/mfa" element={<MfaVerify />} />
        <Route path="/login/mfa/enrol" element={<MfaEnrol />} />
        {/* The Terms gate, like the second factor's enrolment: outside the shell, before any console page. */}
        <Route path={TERMS_ACCEPT_PATH} element={<TermsAcceptPage />} />
        {/* The community edition's first run: its one user is created here (`pages/ce/CeSetupPage.tsx`). */}
        {IS_CE && <Route path="/setup" element={<CeSetupPage />} />}
        {/* Sign-up, password reset by mail, invitations and the legal pages: the cloud's (`routesCloud.tsx`). */}
        {CLOUD?.cloudRoutes.public()}
        <Route element={<RequireAuth />}>
          <Route element={<AuthLayout />}>
            {shellRoutes()}
            {notInEditionRoutes()}
            {/* An unknown path is "Page not found", unless a newer deploy may know it (`UnknownRoute`). In the
                shell, so the sidebar stays a way out; a signed-out visitor signs in first, as for any page. */}
            <Route path="*" element={<UnknownRoute />} />
          </Route>
        </Route>
      </Routes>
    </Suspense>
    </ListDetailProvider>
  )
}
