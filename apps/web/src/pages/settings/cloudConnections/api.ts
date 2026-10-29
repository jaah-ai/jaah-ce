// The cloud-connection surface's ONE API client, shared by the Settings table, the Add-node wizard and the
// node-group dialog so they cannot disagree about a URL (shape precedent: `settings/tags/api.ts`). Lives
// under `settings/` though `components/addNode/AddNodeDialog.tsx` reads it: a module lives where the thing
// it describes lives (`components/PortGridSection.tsx` states the rule). Record shapes live in
// `src/types.ts`; this module owns the REQUEST shapes, since no create body is a record minus a field.

import { del, get, post, put } from '../../../api'
import { NETWORKS_TAB_PATHS } from '../../../routeGroups'
import type {
  CloudConnectionCredentialOption,
  CloudConnectionOption,
  CloudConnectionOptions,
  CloudConnectionRow,
  CloudProvider,
  CloudProviderSpec,
} from '../../../types'

/** What a cloud launch form may offer; an EMPTY instance-type allowlist is a real setting (see the type). */
export const listCloudConnectionOptions = () => get<CloudConnectionOptions>('/api/cloud-connections/options')

/* ── The connections table ─────────────────────────────────────────────────────────────────────────
   `region` is required; `subnet_id`/`security_group_id` sent as `''` ask the route to RESOLVE them (the
   region's default VPC + first default subnet; a found-or-created `jaah-node` group) when the row is
   saved; `ami_id` `''`/omitted = resolve at every launch. On CREATE an omitted key (`undefined`, dropped by
   `JSON.stringify`) takes the server default; never send `null`. On UPDATE an omission means "leave
   alone" (`exclude_unset=True`), so clearing sends the empty VALUE (`''` for `description`). `root_volume_gb` cannot be cleared (8 GB floor). The retired flags
   (`associate_public_ip`, `join_tailnet`) are NOT accepted: the route's StrictModel refuses unknown keys. */
export type CloudConnectionBody = {
  name: string
  description?: string
  provider?: CloudProvider
  credential_id: string
  region: string
  ami_id?: string
  subnet_id?: string
  extra_subnet_ids?: string[]
  security_group_id?: string
  root_volume_gb?: number
  instance_profile?: string
  /** The provider's own network fields (`CloudProviderSpec.fields`), `''`/`[]` = resolve on save. */
  network?: Record<string, unknown>
}

/** The connections table's react-query key; writes invalidate it. */
export const CLOUD_CONNECTIONS_KEY = ['cloud-connections'] as const
export const listCloudConnections = () => get<CloudConnectionRow[]>('/api/cloud-connections')

export const createCloudConnection = (body: CloudConnectionBody) =>
  post<CloudConnectionRow>('/api/cloud-connections', body)

/** A PARTIAL update: only present keys are touched (a body omitting a blanked field saves 200 and changes
 *  nothing). Shares the create's shape. */
export const updateCloudConnection = (id: string, body: CloudConnectionBody) =>
  put<CloudConnectionRow>(`/api/cloud-connections/${id}`, body)

export const deleteCloudConnection = (id: string) => del(`/api/cloud-connections/${id}`)

/* ── Lookups the dialog's selects are fed from ────────────────────────────────────────────────────
   Regions are a static catalogue (no credential). Subnets and security groups are read from AWS under the
   chosen credential; an AWS refusal comes back 502 with AWS's own sentence as the detail, which the field
   shows verbatim (nothing here could say it better). */
export type RegionOption = { code: string; name: string }

export type SubnetOption = {
  id: string
  vpc_id: string
  availability_zone: string
  cidr_block: string
  is_default: boolean
  /** The `Name` tag, `''` when untagged. */
  name: string
}

export type SecurityGroupOption = { id: string; name: string; vpc_id: string; description: string }

export const listRegions = () => get<RegionOption[]>('/api/cloud-connections/lookup/regions')

const lookupQuery = (credentialId: string, region: string) =>
  `credential_id=${encodeURIComponent(credentialId)}&region=${encodeURIComponent(region)}`

export const listSubnets = (credentialId: string, region: string) =>
  get<SubnetOption[]>(`/api/cloud-connections/lookup/subnets?${lookupQuery(credentialId, region)}`)

export const listSecurityGroups = (credentialId: string, region: string) =>
  get<SecurityGroupOption[]>(`/api/cloud-connections/lookup/security-groups?${lookupQuery(credentialId, region)}`)

/** The clouds the caller's plan offers, each with its connection form; a cloud off the plan is absent. `orgId`: an
 *  operator's dialog for another org reads THAT org's plan. */
export const listCloudProviders = (orgId?: string) => get<CloudProviderSpec[]>(
  `/api/cloud-connections/providers${orgId ? `?org_id=${encodeURIComponent(orgId)}` : ''}`)

export type LookupOption = { value: string; label: string }

/** One connection field's dropdown, any cloud (`lookup/{field}`): `region` needs only the credential; any other
 *  field also the region and the values of the fields that narrow it (`parent`). */
export function lookupConnectionField(
  field: string, provider: CloudProvider, credentialId: string, region = '', parents: Record<string, string> = {},
) {
  const query = new URLSearchParams({ provider, credential_id: credentialId })
  if (region) query.set('region', region)
  for (const [name, value] of Object.entries(parents)) if (value) query.set(name, value)
  return get<LookupOption[]>(`/api/cloud-connections/lookup/${encodeURIComponent(field)}?${query.toString()}`)
}

/** The region a connection opens on: the credential's own, else AWS's default. Spelled once for the
 *  dialog and the inline credential form (the registry's AWS `region` field defaults the same way). */
export const DEFAULT_REGION = 'us-east-1'

export function initialRegion(credential: CloudConnectionCredentialOption | undefined): string {
  return credential?.region || DEFAULT_REGION
}

/* ── How a connection is named, two spellings for two surfaces ──────────────────────────────────
   `connectionLabel` ("prod-eu · AWS · eu-west-1") is the LAUNCH pickers' (the Add-node wizard, the node-group
   dialog): the operator is picking where a node runs, and the account is implied by the org. `connectionKnobLabel`
   ("prod · eu-west-1 · jaah-production") is the operator's RENTAL knob's (`pages/Settings.tsx`): there the operator
   is picking which ACCOUNT bills, so the credential is named and two "prod" rows on different accounts stay apart. */
export function connectionLabel(connection: CloudConnectionOption): string {
  return `${connection.name} · ${connection.provider_label || PROVIDER_LABELS[connection.provider] || connection.provider} · ${connection.region}`
}

export function connectionKnobLabel(connection: CloudConnectionOption): string {
  return `${connection.name} · ${connection.region} · ${connection.credential_name ?? connection.credential_id}`
}

/** The provider's display name; the three the column admits. */
export const PROVIDER_LABELS: Readonly<Record<CloudProvider, string>> = {
  aws: 'AWS',
  gcp: 'GCP',
  azure: 'Azure',
}

/* Where the connections live (Directory > Networks > Connections, `pages/nodes/NetworksPage.tsx`), spelled once for its
   surfaces: the prose path and the link that opens it. */
export const CLOUD_CONNECTIONS_PATH = 'Networks > Connections'
export const CLOUD_CONNECTIONS_URL = NETWORKS_TAB_PATHS.networks
