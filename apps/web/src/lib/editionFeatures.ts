// GENERATED from packages/editions/features.toml by tools/editions/edition_map.py — do not edit.
/* The ce edition's features: guard a feature by its name (`isEnabled('<slug>')`). Import-free. */

export const LABELS = {
  billing: 'Billing and plans',
  hostings: 'Hostings',
  cloud_nodes: 'Cloud nodes',
  node_groups: 'Node groups',
  placement_groups: 'Node groups',
  multi_node_hosts: 'Multi-Node Hosts',
  nodelink: 'NodeLink shared folders',
  microvm: 'MicroVM sessions',
  gha_runners: 'GitHub Actions runners',
  apt_self_upgrade: 'Automatic node upgrades',
  tailnet: 'Private network',
  node_log_archive: 'Node log archive',
  cpu_profiling: 'CPU profiling',
  knowledge_base: 'Knowledge base',
  support_assistant: 'Help assistant',
  site_chat: 'Website chat',
  project_memory: 'Project memory',
  full_doctrine: 'Full agent doctrine',
  compliance: 'Compliance',
  merge_lane: 'Merge lane',
  jaah_ai_credit: 'Jaah AI credit',
  sso: 'Single sign-on',
  scim: 'SCIM provisioning',
  siem_export: 'SIEM export',
  tenant_mail: 'Org email',
  system_mail: 'System email',
  data_residency: 'Data residency',
  org_transfer: 'Org transfer',
  multi_org_tenancy: 'Multiple orgs',
  signup: 'Sign-up',
  users_invites: 'Users and invitations',
  oauth_signin: 'Sign in with GitHub, Google or Microsoft',
  forge_connect: 'Connect with GitHub, GitLab, Bitbucket or Azure DevOps',
  event_triggers: 'Event triggers',
  slack: 'Slack',
  telegram: 'Telegram',
  legal_acceptance: 'Terms acceptance',
  customers: 'Customers',
  easy_mode_hosting: 'Easy Mode app hosting',
  control_plane_view: 'Control plane view',
  live_cast: 'Live Cast',
  operator_dashboard: 'Operator dashboard',
  model_prices: 'Model pricing',
  trust_center: 'Trust center',
  site_enquiry: 'Website enquiries',
  session_efficiency: 'Session efficiency',
  aws: 'Amazon Web Services',
  exec_dashboard: 'Command center',
} as const satisfies Readonly<Record<string, string>>

export type FeatureName = keyof typeof LABELS

export type EditionName = 'cloud' | 'ce'

/** Each edition's features, the map's columns (`test/edition.ts` switches editions with them). */
export const BY_EDITION: Readonly<Record<EditionName, ReadonlySet<FeatureName>>> = {
  cloud: new Set(Object.keys(LABELS) as FeatureName[]),
  ce: new Set<FeatureName>(),
}

export const ENABLED: ReadonlySet<FeatureName> = BY_EDITION['ce']

export function isEnabled(feature: FeatureName): boolean {
  return ENABLED.has(feature)
}

/** Each feature's console paths, which render the upsell in an edition without it (`test/edition.ts` derives an
 *  edition's `REMOVED_CONSOLE_ROUTES` from them). */
export const CONSOLE_ROUTES: Readonly<Partial<Record<FeatureName, readonly string[]>>> = {
  billing: ['/org/billing/*', '/settings/billing', '/products/*'],
  cloud_nodes: ['/nodes/*', '/networks', '/settings/nodes'],
  node_groups: ['/nodes/groups/*'],
  multi_node_hosts: ['/nodes/hosts/*', '/hosts/*'],
  nodelink: ['/nodes/nodelinks/*'],
  support_assistant: ['/support/*'],
  compliance: ['/compliance', '/compliance/*'],
  sso: ['/settings/sso'],
  scim: ['/settings/scim'],
  multi_org_tenancy: ['/settings/organizations'],
  users_invites: ['/users/*', '/groups/*', '/access'],
  customers: ['/customers/*'],
}

/** The console paths this edition renders as the upsell for the feature it lacks. */
export const REMOVED_CONSOLE_ROUTES: readonly { path: string; feature: FeatureName }[] = [
  { path: '/org/billing/*', feature: 'billing' },
  { path: '/settings/billing', feature: 'billing' },
  { path: '/products/*', feature: 'billing' },
  { path: '/nodes/*', feature: 'cloud_nodes' },
  { path: '/networks', feature: 'cloud_nodes' },
  { path: '/settings/nodes', feature: 'cloud_nodes' },
  { path: '/nodes/groups/*', feature: 'node_groups' },
  { path: '/nodes/hosts/*', feature: 'multi_node_hosts' },
  { path: '/hosts/*', feature: 'multi_node_hosts' },
  { path: '/nodes/nodelinks/*', feature: 'nodelink' },
  { path: '/support/*', feature: 'support_assistant' },
  { path: '/compliance', feature: 'compliance' },
  { path: '/compliance/*', feature: 'compliance' },
  { path: '/settings/sso', feature: 'sso' },
  { path: '/settings/scim', feature: 'scim' },
  { path: '/settings/organizations', feature: 'multi_org_tenancy' },
  { path: '/users/*', feature: 'users_invites' },
  { path: '/groups/*', feature: 'users_invites' },
  { path: '/access', feature: 'users_invites' },
  { path: '/customers/*', feature: 'customers' },
]
