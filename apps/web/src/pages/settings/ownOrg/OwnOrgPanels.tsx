import { type ComponentType, Fragment, type JSX, type ReactNode } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { type Admin, useAuth } from '../../../auth'
import { FONT_WEIGHT } from '../../../theme'
import { isEnabled } from '../../../lib/editionFeatures'
import { editionModule } from '../../../lib/editionModule'
import { useOwnOrgId } from '../../../hooks/useOwnOrgId'
import { isOrgAdminSeat } from '../../../lib/orgAdmin'
import { type PlanSwitch, planSurfaceShown, planSwitchOn, SLACK_SWITCH } from '../../../lib/planSwitches'
import IpAllowlistSection from '../../../components/security/IpAllowlistSection'
import type { Org } from '../../../types'
import Banner from '../../../ui/Banner'
import { HipaaChip } from '../../../ui/MarkChip'
import LoadingState from '../../../ui/LoadingState'
import { monoSx } from '../../../ui/listStyles'
import DomainsTab from '../../orgs/DomainsTab'
import BackgroundJobsTab from '../../orgs/BackgroundJobsTab'
import RoutingTab from '../../orgs/RoutingTab'
import { GmailConnectReturnBanner } from '../email/connectReturn'
import EmailSection from '../email/EmailSection'
import { getOrg } from '../../orgs/api'
import { apiDetail } from '../../../api'

/* Single sign-on's (`sso`) and SCIM's (`scim`) cards, loaded where present (`lib/editionModule.ts`): an edition without
 * the feature leaves their files out (packages/editions/features.toml). */
type SignInCard = ComponentType<{ org: Org; isOperator: boolean; onOrgSaved: (org: Org) => void }>
type OrgCard = ComponentType<{ orgId: string }>
const SsoTab = editionModule(isEnabled('sso'),
  () => import.meta.glob<SignInCard>('../../orgs/SsoTab.tsx', { eager: true, import: 'default' }))
const SsoSection = editionModule(isEnabled('sso'),
  () => import.meta.glob<OrgCard>('../../../components/security/SsoSection.tsx', { eager: true, import: 'default' }))
const ScimSection = editionModule(isEnabled('scim'),
  () => import.meta.glob<OrgCard>('../../../components/security/ScimSection.tsx', { eager: true, import: 'default' }))
/* Audit export (`siem_export`), the staging org (`multi_org_tenancy`) and the org's MicroVM size (`microvm`): the same
 * mechanism, and each card is offered only with its feature. */
const SiemExportSection = editionModule(isEnabled('siem_export'),
  () => import.meta.glob<OrgCard>('../../../components/security/SiemExportSection.tsx', { eager: true, import: 'default' }))
const StagingTab = editionModule(isEnabled('multi_org_tenancy'),
  () => import.meta.glob<ComponentType>('../../orgs/StagingTab.tsx', { eager: true, import: 'default' }))
type MicrovmCard = ComponentType<{ org: Org; canEdit: boolean; onOrgSaved: (org: Org) => void }>
const MicrovmTab = editionModule(isEnabled('microvm'),
  () => import.meta.glob<MicrovmCard>('../../orgs/MicrovmTab.tsx', { eager: true, import: 'default' }))
/* The org's Slack workspace card and its Connect return banner (`slack`): the same mechanism. */
type SlackCards = { default: ComponentType; SlackConnectReturnBanner: ComponentType }
const slackCards = editionModule(isEnabled('slack'),
  () => import.meta.glob<SlackCards>('../../../components/SlackWorkspaceCard.tsx', { eager: true }))
const SlackWorkspaceCard = slackCards?.default
const SlackConnectReturnBanner = slackCards?.SlackConnectReturnBanner

/** The own-org read's query key (unpersisted: `queryClient.ts`). */
export const OWN_ORG_KEY = 'own-org'

/* The viewer's own org as Settings › Organizations cards (one card per subject, each found by search,
 * in place of one card holding a tab strip): its name, who joins by email domain, how its members sign in, its
 * SAML/OIDC identity provider, SCIM, the networks they may sign in from, where its activity events go, its Slack
 * workspace, the Gmail mailboxes it sends from, its staging org, its MicroVM size and who its background jobs run as. Who has access is Directory › People › Access
 * (`pages/users/UsersPage.tsx`). The operator manages every org on its customer page (`pages/customers/`). */

export type OwnOrgPanel = 'orgSummary' | 'orgDomains' | 'orgSignIn' | 'orgSso' | 'orgScim' | 'orgIpAllowlist' | 'orgAuditExport' | 'orgEmail' | 'orgSlack' | 'orgStaging' | 'orgMicrovm' | 'orgRouting' | 'orgBackgroundJobs'

/* An Org Owner/Admin's alone, each behind its plan switch: off the plan a card is not offered at all. */
const ADMIN_PANELS: readonly [OwnOrgPanel, PlanSwitch][] = [
  ['orgSso', 'sso_enabled'], ['orgScim', 'scim_enabled'], ['orgIpAllowlist', 'ip_allowlist_enabled'],
  ['orgAuditExport', 'siem_export_enabled'], ['orgEmail', 'tenant_email_allowed'], ['orgSlack', SLACK_SWITCH],
  ['orgStaging', 'staging_org_enabled'],
]

/** Which own-org cards this viewer is offered: none without an org bound or an Org Owner/Admin seat. */
export function ownOrgPanelsShown(admin: Admin | null | undefined): Record<OwnOrgPanel, boolean> {
  const base = isOrgAdminSeat(admin) && Boolean(admin?.org?.id)
  const adminOn = (panel: OwnOrgPanel) => {
    const name = ADMIN_PANELS.find(([p]) => p === panel)?.[1]
    return base && name !== undefined && planSwitchOn(admin, name)
  }
  return {
    orgSummary: base,
    orgDomains: base,
    orgSignIn: base && isEnabled('sso'),
    orgSso: isEnabled('sso') && adminOn('orgSso'),
    orgScim: isEnabled('scim') && adminOn('orgScim'),
    orgIpAllowlist: adminOn('orgIpAllowlist'),
    orgAuditExport: SiemExportSection !== undefined && adminOn('orgAuditExport'),
    orgEmail: adminOn('orgEmail'),
    orgSlack: adminOn('orgSlack'),
    // Staging is a tenant's: the operator plane has no plan to share.
    orgStaging: StagingTab !== undefined && adminOn('orgStaging') && admin?.is_operator !== true,
    orgMicrovm: MicrovmTab !== undefined && base && planSurfaceShown(admin, 'microvm_allowed'),
    // Shown off the plan too, its controls disabled under the reason: an Admin learns why it is off.
    orgRouting: base,
    orgBackgroundJobs: base,
  }
}

/* A panel of the viewer's own org: drawn with its id, nothing with no org bound (`Settings.tsx::panelShown`). */
export function OwnOrgGate({ children }: { children: (orgId: string) => ReactNode }): ReactNode {
  const orgId = useOwnOrgId()
  return orgId ? children(orgId) : null
}

/* One read of the org for every card that needs the record, and one place a card's save lands. */
function useOwnOrg(): { org: Org | undefined; error: string; setOrg: (org: Org) => void } {
  const id = useOwnOrgId() ?? ''
  const client = useQueryClient()
  const key = [OWN_ORG_KEY, id]
  const query = useQuery({ queryKey: key, queryFn: () => getOrg(id), enabled: id !== '', refetchOnMount: 'always' })
  const error = query.error ? apiDetail(query.error, 'Failed to load settings.') : ''
  /* A card's form copies the record once, on its first draw: never seed it from a copy older than this mount, or a
     Save would write that copy back (the sign-in provider, HIPAA). */
  const org = query.isFetchedAfterMount ? query.data : undefined
  return { org, error, setOrg: next => client.setQueryData(key, next) }
}

/* A card's body once the org has loaded, keyed on the org so a form never carries one org's draft into another. */
function WithOrg({ children }: { children: (org: Org, setOrg: (org: Org) => void) => ReactNode }): JSX.Element {
  const { org, error, setOrg } = useOwnOrg()
  if (error) return <Banner severity="error">{error}</Banner>
  if (!org) return <LoadingState rows={2} />
  return <Fragment key={org.id}>{children(org, setOrg)}</Fragment>
}

/* The org's name and slug. Slack's and Gmail's return errors land here when the plan leaves the feature out
   (`off_plan`): no Slack or Email card says it then. */
function Summary({ slackShown, emailShown }: { slackShown: boolean; emailShown: boolean }): JSX.Element {
  return (
    <>
      {!slackShown && SlackConnectReturnBanner && <Box sx={{ mb: 1.5, '&:empty': { display: 'none' } }}><SlackConnectReturnBanner /></Box>}
      {!emailShown && <Box sx={{ mb: 1.5, '&:empty': { display: 'none' } }}><GmailConnectReturnBanner /></Box>}
      <WithOrg>
      {org => (
        <Box>
          <Typography variant="body1" sx={{ fontWeight: FONT_WEIGHT.semibold }}>
            {org.name}
            {org.hipaa?.enabled && <HipaaChip baaDate={org.hipaa.baa_effective_date} />}
          </Typography>
          <Typography variant="body2" sx={{ color: 'text.secondary' }}>
            Slug <Box component="span" data-testid="org-slug" sx={monoSx}>{org.slug}</Box>
          </Typography>
        </Box>
      )}
      </WithOrg>
    </>
  )
}

/** One card's body (Settings frames it, titled by its section). */
export function OwnOrgPanelBody({ panel }: { panel: OwnOrgPanel }): JSX.Element | null {
  const { admin } = useAuth()
  const isOperator = admin?.is_operator === true
  const orgId = admin?.org?.id ?? ''
  switch (panel) {
    case 'orgSummary':
      return <Summary slackShown={ownOrgPanelsShown(admin).orgSlack} emailShown={ownOrgPanelsShown(admin).orgEmail} />
    case 'orgDomains':
      return <WithOrg>{(org, setOrg) => <DomainsTab org={org} isOperator={isOperator} onOrgSaved={setOrg} />}</WithOrg>
    case 'orgSignIn':
      if (!isEnabled('sso') || !SsoTab) return null
      return <WithOrg>{(org, setOrg) => <SsoTab org={org} isOperator={isOperator} onOrgSaved={setOrg} />}</WithOrg>
    case 'orgSso':
      if (!isEnabled('sso') || !SsoSection) return null
      return <SsoSection orgId={orgId} />
    case 'orgScim':
      if (!isEnabled('scim') || !ScimSection) return null
      return <ScimSection orgId={orgId} />
    case 'orgIpAllowlist':
      return <IpAllowlistSection orgId={orgId} />
    case 'orgAuditExport':
      return SiemExportSection ? <SiemExportSection orgId={orgId} /> : null
    case 'orgEmail':
      return <EmailSection orgId={orgId} />
    case 'orgSlack':
      return SlackWorkspaceCard ? <SlackWorkspaceCard /> : null
    case 'orgStaging':
      return StagingTab ? <StagingTab /> : null
    case 'orgMicrovm':
      if (!MicrovmTab) return null
      return <WithOrg>{(org, setOrg) => <MicrovmTab org={org} canEdit onOrgSaved={setOrg} />}</WithOrg>
    case 'orgRouting':
      return <WithOrg>{(org, setOrg) => <RoutingTab org={org} onOrgSaved={setOrg} />}</WithOrg>
    case 'orgBackgroundJobs':
      return <WithOrg>{(org, setOrg) => <BackgroundJobsTab org={org} onOrgSaved={setOrg} />}</WithOrg>
  }
}
