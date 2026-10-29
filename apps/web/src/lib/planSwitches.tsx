import { Link, Navigate, Outlet, useLocation } from 'react-router-dom'
import MuiLink from '@mui/material/Link'
import ActAsOrgPicker from '../components/ActAsOrgPicker'
import InterstitialPanel from '../ui/InterstitialPanel'
import { ApiError } from '../api'
import { type Admin, useAuth } from '../auth'
import { useActingAs } from './actAs'
import { isEnabled } from './editionFeatures'
import { SUBSCRIPTION_PAGE } from './companySetup'
import { isOrgAdminSeat } from './orgAdmin'
import type { NodeOs } from './nodeOs'
import { agentsOn, registryOn } from './providerGrid'
import type { CloudProvider } from '../types'

/* THE ORG'S PLAN SWITCHES, off the identity payload (`routes/auth.py`, read through `plan_terms`). The one
 * reader of these session flags: tabs, route guards and pickers ask here, never `admin?.x`. Strictly
 * `true`: absent (an older server, no `me` yet) is OFF, every switch's default. Presentation only — the API
 * refuses what a switch turns off on its own. Two gates are no switch: Agents and ACP registry agents are read off
 * the org's provider grid (`lib/providerGrid.ts`, `GridGate`), the same way wherever a switch is asked. */

export type PlanSwitch =
  | GridGate | 'playbooks_enabled' | 'org_templates_enabled' | 'jaah_ai_enabled'
  | 'nodelink_enabled' | 'nodevault_enabled' | 'microvm_allowed' | 'github_runners_allowed'
  | 'ubuntu_nodes_allowed' | 'macos_nodes_allowed' | 'windows_nodes_allowed' | 'project_memory_enabled'
  | 'audit_enabled' | 'session_transcripts_enabled' | 'tenant_email_allowed' | CloudSwitch | EnterpriseSwitch

/** Your Cloud per provider: whether the org may connect and launch on its own AWS, GCP or Azure. */
export type CloudSwitch = 'aws_cloud_allowed' | 'gcp_cloud_allowed' | 'azure_cloud_allowed'

/** The switch that gates each cloud — the twin of the API's `plan_terms.CLOUD_SWITCHES`. */
export const CLOUD_SWITCH: Record<CloudProvider, CloudSwitch> = {
  aws: 'aws_cloud_allowed',
  gcp: 'gcp_cloud_allowed',
  azure: 'azure_cloud_allowed',
}

/** The gates read off the provider grid, not a switch: Agents (the SDK row or any ACP registry row on) and ACP
 *  registry agents (any `acp:` row on). The roster carries the grid per org (`OrgOut.providers`). */
export type GridGate = 'agents' | 'acp_registry'

const GRID_GATE: Record<GridGate, (p: { providers?: string[][] } | null | undefined) => boolean> = {
  agents: agentsOn,
  acp_registry: registryOn,
}

const isGridGate = (name: string): name is GridGate => Object.hasOwn(GRID_GATE, name)

/** Is a grid gate on for anything holding a provider grid (the identity, a roster org)? */
export function gridGateOn(p: { providers?: string[][] } | null | undefined, name: GridGate): boolean {
  return GRID_GATE[name](p)
}

/** The switches the operator org roster carries per org (`OrgOut.plan_switches`, `plan_terms.CONSOLE_SWITCHES`):
 *  NodeLink and NodeVault are read apart, for the identity payload alone. */
export type RosterSwitch = Exclude<PlanSwitch, 'nodelink_enabled' | 'nodevault_enabled' | GridGate>

/** The switches added with plan inheritance: Slack, then the eight enterprise features. */
export type EnterpriseSwitch =
  | 'slack_enabled' | 'sso_enabled' | 'scim_enabled' | 'siem_export_enabled' | 'data_residency_enabled'
  | 'ip_allowlist_enabled' | 'org_retention_enabled' | 'staging_org_enabled' | 'budgets_enabled'

export function planSwitchOn(admin: Admin | null | undefined, name: PlanSwitch): boolean {
  return isGridGate(name) ? gridGateOn(admin, name) : admin?.[name] === true
}

export function usePlanSwitch(name: PlanSwitch): boolean {
  return planSwitchOn(useAuth().admin, name)
}

/** A plan feature's SURFACE (a tab, a strip entry): hidden from a tenant while its switch is off; the operator
 *  sees every surface whatever its own org's plan says. */
export function planSurfaceShown(admin: Admin | null | undefined, name: PlanSwitch): boolean {
  return admin?.is_operator === true || planSwitchOn(admin, name)
}

/** A plan-gated route's refusal: the 403 an API door answers while the org's plan leaves the feature out (e.g.
 *  the project memory routes). A query passes it to `retry` (never retried) and shows a calm notice, not an error. */
export const offPlan = (err: unknown): boolean => err instanceof ApiError && err.status === 403

/** A plan-gated query's options that make the refusal the answer while it stays cached: never retried, and not
 *  asked again on a remount (re-entering its tab), a refocus or a reconnect — each would log one more console 403.
 *  Any other failure keeps the defaults. */
const askAgain = (query: { state: { error: unknown } }) => !offPlan(query.state.error)

export const OFF_PLAN_ONCE = {
  retry: (count: number, err: Error) => !offPlan(err) && count < 3,
  retryOnMount: askAgain,
  refetchOnWindowFocus: askAgain,
  refetchOnReconnect: askAgain,
} as const

/** What each switch is called where a reader meets it — the web's one home for these labels (the API's twin is
 *  `GET /api/billing/plans`' `switch_catalogue`). */
export const FEATURE_NAME: Record<PlanSwitch, string> = {
  agents: 'Agents',
  acp_registry: 'ACP registry agents',
  jaah_ai_enabled: 'Jaah AI',
  playbooks_enabled: 'Playbooks',
  org_templates_enabled: 'Templates (org)',
  nodelink_enabled: 'NodeLink',
  nodevault_enabled: 'NodeVault',
  microvm_allowed: 'MicroVM',
  github_runners_allowed: 'GitHub Runners',
  ubuntu_nodes_allowed: 'Ubuntu nodes',
  macos_nodes_allowed: 'macOS nodes',
  windows_nodes_allowed: 'Windows nodes',
  project_memory_enabled: 'Project memory',
  audit_enabled: 'Audit',
  session_transcripts_enabled: 'Session transcripts',
  tenant_email_allowed: 'Tenant email',
  aws_cloud_allowed: 'Your Cloud: AWS',
  gcp_cloud_allowed: 'Your Cloud: GCP',
  azure_cloud_allowed: 'Your Cloud: Azure',
  slack_enabled: 'Slack',
  sso_enabled: 'SSO',
  scim_enabled: 'SCIM',
  siem_export_enabled: 'SIEM export',
  data_residency_enabled: 'Data residency',
  ip_allowlist_enabled: 'IP allowlist',
  org_retention_enabled: 'Retention',
  staging_org_enabled: 'Staging org',
  budgets_enabled: 'Budgets',
}

/** The enterprise switches in display order, each with its label (`FEATURE_NAME`). Slack lists under Features;
 *  the other eight form their own group in the plan editor and the org's Overrides tab. */
export const SLACK_SWITCH = 'slack_enabled' satisfies EnterpriseSwitch
export const ENTERPRISE_SWITCHES = (
  ['sso_enabled', 'scim_enabled', 'siem_export_enabled', 'data_residency_enabled', 'ip_allowlist_enabled',
    'org_retention_enabled', 'staging_org_enabled', 'budgets_enabled'] as const satisfies readonly EnterpriseSwitch[]
).map(name => ({ name, label: FEATURE_NAME[name] }))

/** The switch that gates registering a self-hosted node of each OS — the twin of the API's `plan_terms.NODE_OS_SWITCHES`. */
export const NODE_OS_SWITCH: Record<NodeOs, PlanSwitch> = {
  ubuntu: 'ubuntu_nodes_allowed',
  macos: 'macos_nodes_allowed',
  windows: 'windows_nodes_allowed',
}

/** The route half of a switch, twin of its tab's gate: while it is off the pages are unreachable by URL too.
 *  Holds while the identity is loading or still resolving, as `components/RequireFeature.tsx` does. Says
 *  "not on your plan" in place, never `/no-access`: that page asks an administrator for a grant, and no
 *  grant opens a feature the plan leaves out. `to` sends a page that opens as a pane beside a guarded list
 *  there instead, so the sentence shows once; the hop carries the switch as `offPlanArrival` for a landing page
 *  that is not itself guarded to explain it. An operator not acting as an org gets the org picker instead: the
 *  feature runs inside one org and the operator plane's own plan is not the answer (`planSurfaceShown`). The
 *  picker lists the live tenant orgs whose plan has the feature (the roster's `plan_switches`, or
 *  its `providers` for a `GridGate`). `operator`: the
 *  feature reads across orgs for the operator, who passes whatever the plan says (`planSurfaceShown`'s terms). */
export function RequirePlanSwitch({ name, to, operator = false }: { name: RosterSwitch | GridGate; to?: string; operator?: boolean }) {
  const { admin, loading, resolving } = useAuth()
  const acting = useActingAs()
  if (loading) return null
  if (!(operator ? planSurfaceShown(admin, name) : planSwitchOn(admin, name))) {
    if (resolving) return null
    if (to) return <Navigate to={to} replace state={offPlanState(name)} />
    // Without Multiple orgs the operator's one org is the only one: no other org to act as.
    if (admin?.is_operator === true && !acting && isEnabled('multi_org_tenancy')) return <PlanOrgPicker name={name} />
    return <NotOnPlan name={name} />
  }
  return <Outlet />
}

/** The history state a hop away from an off-plan address carries: the switch that sent the reader. */
export const offPlanState = (name: PlanSwitch) => ({ offPlan: name })

/** The switch whose off-plan address sent the reader here (`RequirePlanSwitch`'s `to`), else null. */
export function useOffPlanArrival(): PlanSwitch | null {
  const state: unknown = useLocation().state
  const name = (state as { offPlan?: unknown } | null)?.offPlan
  return typeof name === 'string' && Object.hasOwn(FEATURE_NAME, name) ? name as PlanSwitch : null
}

function PlanOrgPicker({ name }: { name: RosterSwitch | GridGate }) {
  const feature = FEATURE_NAME[name]
  // Every plural label ends in a lowercase "s" (Agents, ACP registry agents, Playbooks …); no singular one does.
  const verb = feature.endsWith('s') ? 'run' : 'runs'
  return (
    <ActAsOrgPicker
      testId="plan-org-picker"
      actAsTestId="plan-act-as"
      title={`${feature} ${verb} inside one org`}
      body={view => `${view} is a view, not an org. Act as an org to use ${feature}.`}
      eligible={org => (isGridGate(name) ? gridGateOn(org, name) : org.plan_switches?.[name] === true)}
      none={`has ${feature}`}
      noneHint={<>Turn it on in an org's Overrides{isGridGate(name) && ' › Providers'} under <Link to="/customers">Customers</Link>.</>}
    />
  )
}

/* The way out names who can change the plan: the Subscription page is the org admin's
   (`RequireSuperadmin orgAdmin`), so anyone else is pointed at one — `pages/agents/agentsTabs.tsx`'s
   `RegistryOffPlanNote` words it the same. */
export function NotOnPlan({ name }: { name: PlanSwitch }) {
  const { admin } = useAuth()
  if (!isEnabled('billing')) {
    return <InterstitialPanel testId="not-on-plan" title="Not in this edition" body={`${FEATURE_NAME[name]} isn’t in Jaah Community Edition.`} />
  }
  return (
    <InterstitialPanel
      testId="not-on-plan"
      title="Not on your plan"
      body={
        <>
          Your plan doesn't include {FEATURE_NAME[name]}.{' '}
          {isOrgAdminSeat(admin)
            ? <MuiLink component={Link} to={SUBSCRIPTION_PAGE}>See plans</MuiLink>
            : 'Ask an admin to change the plan.'}
        </>
      }
    />
  )
}
