// Networks > NodeLink's ONE API client: the org's NodeLinks (`routes/nodelinks.py`) and the storage they sit on
// (`routes/org_shared_store.py`). Every route acts on the CALLER'S OWN org; the operator's read-only view of
// another org's NodeLink passes that org's id to the reads that take one (`orgId`).

import { del, get, patch, post, put } from '../../../api'
import { credentialMark } from '../../../ui/credentialMarks'
import { slugFrom } from '../../../lib/slug'
import type { CloudProvider, NodeLinkStore, OrgRef } from '../../../types'
import { type Credential, getCredential, loadCredentialOptions } from '../../credentials/api'
import type { CredentialOption } from '../../../ui/CredentialSelect'
import { NETWORKS_TAB_PATHS } from '../../../routeGroups'

/** The NodeLink list (Directory > Networks > NodeLink, `pages/nodes/NetworksPage.tsx`), spelled once for the links to it. */
export const NODELINK_STORE_URL = `${NETWORKS_TAB_PATHS.networks}?tab=nodelink`

/** One NodeLink's editor; `tab` lands on that tab. */
export const nodeLinkUrl = (id: string, tab?: 'storage' | 'nodes') =>
  `/nodes/nodelinks/${encodeURIComponent(id)}${tab ? `?tab=${tab}` : ''}`

/** The operator's read-only view of another org's NodeLink: its editor, reading as `orgId`. */
export const nodeLinkViewUrl = (id: string, orgId: string) =>
  `/nodes/nodelinks/${encodeURIComponent(id)}?org=${encodeURIComponent(orgId)}`

/** `?org_id=` for a read of another org's (the operator only); nothing for the caller's own. */
const orgQuery = (orgId?: string) => (orgId ? `?org_id=${encodeURIComponent(orgId)}` : '')

/* ── Storage ──────────────────────────────────────────────────────────────────────────────────────
   Many per org, each a file system (created by the CP, or adopted). Chosen per NodeLink in its Storage tab. */
export const STORES_PATH = '/api/org/nodelink-stores'

export const listStores = (orgId?: string) => get<NodeLinkStore[]>(`${STORES_PATH}${orgQuery(orgId)}`)
const storePath = (id: string) => `${STORES_PATH}/${encodeURIComponent(id)}`
/** Re-read the store's AWS facts (the file system's ARN; an adopted store's mount target IP). 502: AWS's text. */
export const refreshStore = (id: string) => post<NodeLinkStore>(`${storePath(id)}/refresh`, {})
/** Rewrite the file system's policy to admit every live NodeLink on it; `access_point_count` is how many it admits. */
export const repairStorePolicy = (id: string) =>
  post<NodeLinkStore & { access_point_count: number }>(`${storePath(id)}/repair-policy`, {})
export type StorePolicy = {
  /** As AWS holds it; null when it has none. */
  policy: string | null
  /** It is the policy Jaah would write now; null while no NodeLink needs one (the org's own policy stands). */
  current: boolean | null
  /** Why it is not current. */
  reason: string | null
  /** The access points it admits. */
  access_point_ids: string[]
}
/** The file system's policy, read live from AWS (read-only), and whether it is current. 502: AWS's text. */
export const getStorePolicy = (id: string) => get<StorePolicy>(`${storePath(id)}/policy`)
/** Run the store on another of the org's AWS credentials. 422: it does not see the file system (AWS's reason). */
export const setStoreCredential = (id: string, credentialId: string) =>
  patch<NodeLinkStore>(storePath(id), { credential_id: credentialId })
/** Why no route can reach the file system's mount targets, read live from AWS; `warning` null when fine. */
export const getStoreNetwork = (id: string) => get<{ warning: string | null }>(`${storePath(id)}/network`)
/** An adopted store's mount target ('' = none, nodes resolve by DNS). 422: not on its file system. */
export const setStoreMountTarget = (id: string, mountTargetId: string) =>
  patch<NodeLinkStore>(storePath(id), { mount_target_id: mountTargetId })
/** Delete a store no NodeLink uses (409 otherwise, or while `provisioning`). `deleteInCloud` false: the row only,
 *  the cloud object stays (204). True (`deletable_in_cloud` only, else 422): `null` once gone (204), else the row
 *  `deleting` (202), finished by the `nodelink-provision` cron; repeating it retries the pass. */
export const deleteStore = (id: string, deleteInCloud: boolean) =>
  del<NodeLinkStore | null>(`${storePath(id)}?delete_in_cloud=${deleteInCloud}`).then(row => row ?? null)

/** A size in the cloud's own unit: whole TiB as TiB, else GiB. */
export const sizeText = (gib: number) => (gib >= 1024 && gib % 1024 === 0 ? `${gib / 1024} TiB` : `${gib} GiB`)

/* ── Use existing ─────────────────────────────────────────────────────────────────────────────────
   The org's own EFS objects, read from AWS under the chosen credential. An AWS refusal is a 502 whose detail
   is plain prose, shown as is. */
export type ExistingFileSystem = { id: string; name: string | null; life_cycle_state: string }

export type ExistingAccessPoint = {
  id: string
  file_system_id: string
  name: string | null
  root_directory: string
  uid: number | null
  gid: number | null
  /** Enforced uid = gid = 3000 (any root); `problem` says why not. */
  usable: boolean
  problem: string | null
}

export type ExistingMountTarget = {
  id: string
  file_system_id: string
  ip: string
  subnet_id: string
  availability_zone: string | null
}

export type ExistingOptions = {
  file_systems: ExistingFileSystem[]
  access_points: ExistingAccessPoint[]
  mount_targets: ExistingMountTarget[]
}

export const listExistingStores = (credentialId: string, region: string) =>
  get<ExistingOptions>(
    `${STORES_PATH}/existing?credential_id=${encodeURIComponent(credentialId)}&region=${encodeURIComponent(region)}`,
  )

export type CreateStoreBody = {
  name: string
  credential_id: string
  region: string
  subnet_id: string
  ingress_cidr: string
}

export type ExistingStoreBody = {
  name: string
  credential_id: string
  region: string
  file_system_id: string
  /** Omitted = none: nodes resolve the file system by DNS. */
  mount_target_id?: string
}

/* ── Storage on a cloud connection (GCP Filestore, Azure Files) ───────────────────────────────────────
   Placed by one of the org's cloud connections, not by AWS fields. Each cloud on the org's plan, with its minimum
   size and bill (shown before Create) and the org's connections on it. */
export type CloudStorage = {
  provider: CloudProvider
  label: string
  min_size_note: string
  connections: { id: string; name: string; region: string }[]
}
export const CLOUD_STORAGES_KEY = ['nodelink-cloud-storages'] as const
export const listCloudStorages = () => get<CloudStorage[]>(`${STORES_PATH}/clouds`)

/** What a connection's account has to adopt as storage (a Filestore instance, an Azure file share). */
export type ExistingChoice = { id: string; name: string; usable: boolean; problem: string | null }
export const listConnectionExisting = (connectionId: string) =>
  get<ExistingChoice[]>(`${STORES_PATH}/connections/${encodeURIComponent(connectionId)}/existing`)

/** `existing` omitted = Jaah builds it; set = that cloud object adopted. */
export type ConnectionStoreBody = { name: string; connection_id: string; existing?: string }

/** `PUT …/{id}/storage`: exactly one of `store_id` | `create` | `existing` | `connection` | `managed`; `access_point_id`
 *  adopts one (omitted = Jaah creates its own; never with `managed`). Only while the NodeLink has no access
 *  point yet. `managed` starts provisioning at once: the reply is usually already `provisioning`. */
export type NodeLinkStorageBody =
  | { managed: true }
  | { store_id: string; access_point_id?: string }
  | { create: CreateStoreBody }
  | { existing: ExistingStoreBody; access_point_id?: string }
  | { connection: ConnectionStoreBody }

const awsOption = (row: Credential): CredentialOption => ({
  id: row.id,
  name: row.name,
  hint: row.description?.trim() || undefined,
  icon: credentialMark('aws', 'AWS'),
})

/** The caller's `aws` credentials, as `ui/CredentialSelect` wants them. Server-filtered by type and
 *  query — the roster is paginated, so a browser-side filter would silently read one page. */
export const fetchAwsCredentialOptions = (query: string, signal: AbortSignal) =>
  loadCredentialOptions(query, signal, awsOption, { types: ['aws'], sort: true })

/** One AWS credential by id, for the picker to name a value past the page it fetched (`resolveOption`); null for
 *  another type, which the roster never offers either. */
export const resolveAwsCredentialOption = async (id: string, signal: AbortSignal) => {
  const row = await getCredential(id, { signal })
  return row.type === 'aws' ? awsOption(row) : null
}

/* ── NodeLinks ────────────────────────────────────────────────────────────────────────────────────
   The org's NodeLinks (`routes/nodelinks.py`; every rule in `app/nodelinks.py`). A refusal's `detail` is the
   server's own sentence, shown as is. */
export const NODELINKS_PATH = '/api/org/nodelinks'

/** The `{jaah-home}` token every mount path starts with; each node resolves it to its own home. */
export const JAAH_HOME = '{jaah-home}'
/** Where a general NodeLink lands by default (`nodelinks.DEFAULT_PARENT`). */
export const DEFAULT_PARENT = `${JAAH_HOME}/nodelink/`
/** Where a NodeLink shares account sign-ins between nodes (`nodelinks.ACCOUNTS_MOUNT_PATH`). */
export const ACCOUNTS_MOUNT_PATH = `${JAAH_HOME}/auth/.shared`
export type NodeLinkStatus = 'pending' | 'provisioning' | 'ready' | 'failed' | 'deleting'

export type NodeLink = {
  id: string
  name: string
  slug: string
  description: string
  /** The template: `{jaah-home}/…`. */
  mount_path: string
  status: NodeLinkStatus
  /** AWS's error; for a `pending` row, what it is waiting for (served live, `nodelinks.pending_reason`). */
  last_error: string | null
  /** The store's file-system policy admits this NodeLink's access point. */
  in_policy: boolean
  node_ids: string[]
  node_count: number
  created_at?: string | null
  updated_at?: string | null
  /** null until the Storage tab chooses one. */
  store_id: string | null
  /** "Jaah-managed" for a managed store. */
  store_name: string | null
  /** The cloud its storage lives on; null until chosen (absent from an older server = AWS). */
  store_provider?: CloudProvider | null
  store_status: string | null
  /** On the operator's storage: its id is not in `listStores()`, and Refresh/Repair do not apply. */
  store_managed: boolean
  /** A Jaah-managed store's location; null for the org's own. */
  store_location?: ManagedLocation | null
  /** The store's file system (`fs-…`); a Jaah-managed one's for the operator only. */
  store_file_system_id?: string | null
  /** The NodeLink's org, on `listAllOrgsNodeLinks` and another org's `getNodeLink` only. */
  org?: OrgRef | null
}

/** The steps a provision walks, in order (`nodelinks.progress`): every one on EFS; a Filestore has no `policy`, an
 *  Azure share only `file_system` and `access_point`. */
export type ProvisionStepId = 'file_system' | 'mount_target' | 'access_point' | 'policy'
export type ProvisionStepState = 'done' | 'current' | 'waiting' | 'failed'
export type ProvisionStep = { step: ProvisionStepId; state: ProvisionStepState }

export type NodeLinkDetail = NodeLink & {
  access_point_id: string | null
  /** An existing access point the admin picked: a delete leaves it in AWS. */
  access_point_adopted: boolean
  root_directory: string
  /** Outside `{jaah-home}/nodelink/`: may cover a folder nodes already use. */
  shadows_existing: boolean
  /** The nodes a delete still waits on to unmount it. */
  pending_unmount_node_ids: string[]
  /** Where a `provisioning` or `failed` one is, derived live (`nodelinks.progress`); null otherwise. */
  progress?: ProvisionStep[] | null
}

/** The slug the server derives from a name (`entity_shape.unique_org_slug`, before its uniqueness suffix). */
export const slugOf = (name: string) => slugFrom(name, 60) || 'nodelink'

/** The mount path a new NodeLink is offered: `{jaah-home}/nodelink/<slug>`. */
export const defaultMountPath = (name: string) => `${DEFAULT_PARENT}${slugOf(name)}`

/** `nodelinks.shadows_existing`: outside `{jaah-home}/nodelink/`. */
export const shadowsExisting = (path: string) => !path.trim().startsWith(DEFAULT_PARENT)

/** The caller's org's NodeLinks; `orgId` (the operator only) reads that org's instead. */
export const listNodeLinks = (orgId?: string) => get<NodeLink[]>(`${NODELINKS_PATH}${orgQuery(orgId)}`)
/** Every org's NodeLinks the operator's org filter admits, each naming its `org` (the operator only). */
export const listAllOrgsNodeLinks = () => get<NodeLink[]>(`${NODELINKS_PATH}?all_orgs=true`)
/** One NodeLink; `orgId` (the operator only) reads another org's, read-only. */
export const getNodeLink = (id: string, orgId?: string) =>
  get<NodeLinkDetail>(`${NODELINKS_PATH}/${encodeURIComponent(id)}${orgQuery(orgId)}`)

/* ── Jaah-managed ──────────────────────────────────────────────────────────────────────────────────
   The operator's storage, offered to an org whose nodes run on operator infrastructure. The tenant sees no AWS
   id of it but its own NodeLink's access point. */
export type ManagedLocation = 'hosting_vpc' | 'main_vpc'

export const MANAGED_LOCATIONS: readonly ManagedLocation[] = ['hosting_vpc', 'main_vpc']
export const MANAGED_LOCATION_LABEL: Record<ManagedLocation, string> = {
  hosting_vpc: 'Hosting VPC',
  main_vpc: 'Main VPC (bastion)',
}

export type ManagedStorage = {
  available: boolean
  location: ManagedLocation | null
  /** Why it is not offered. */
  reason: string | null
}

/** `nodelinkId` asks for that NodeLink (its nodes decide the location). */
export const getManagedStorage = (nodelinkId?: string) =>
  get<ManagedStorage>(`${NODELINKS_PATH}/managed-storage${nodelinkId ? `?nodelink_id=${encodeURIComponent(nodelinkId)}` : ''}`)

export type NodeLinkBody = { name: string; description?: string; mount_path?: string }

/** Creates it `pending`, with no storage yet. 409: duplicate name or path overlap. */
export const createNodeLink = (body: NodeLinkBody) => post<NodeLinkDetail>(NODELINKS_PATH, body)
export const updateNodeLink = (id: string, body: Partial<NodeLinkBody>) =>
  patch<NodeLinkDetail>(`${NODELINKS_PATH}/${encodeURIComponent(id)}`, body)
/** Choose the NodeLink's storage and access point (`NodeLinkStorageBody`). */
export const setNodeLinkStorage = (id: string, body: NodeLinkStorageBody) =>
  put<NodeLinkDetail>(`${NODELINKS_PATH}/${encodeURIComponent(id)}/storage`, body)
/** Request provisioning: one idempotent pass now — the store (if Jaah creates it), then the access point and
 *  the policy. `provisioning` on return is finished by the server's `nodelink-provision` cron; the page polls. */
export const provisionNodeLink = (id: string) =>
  post<NodeLinkDetail>(`${NODELINKS_PATH}/${encodeURIComponent(id)}/provision`, {})

/** Share subscriptions between nodes, in one call (`routes/nodelinks.py::share_subscriptions`): creates the
 *  NodeLink at `ACCOUNTS_MOUNT_PATH` on `storage`, attaches every node it can go on (`skipped`: the rest, with why)
 *  and requests provisioning. 201 new; 200 the org's existing one there, unchanged. */
/** `node_ids` given: only those among the nodes it can go on (a listed one it cannot is `skipped`). */
export type ShareSubscriptionsBody = { name: string; consent: true; storage: NodeLinkStorageBody; node_ids?: string[] }
/** A node a share did not attach, and why (`name` absent from an older server). */
export type SkippedNode = { node_id: string; name?: string | null; reason: string }
export type ShareSubscriptionsResult = NodeLinkDetail & { skipped: SkippedNode[] }
export const shareSubscriptions = (body: ShareSubscriptionsBody) =>
  post<ShareSubscriptionsResult>(`${NODELINKS_PATH}/share-subscriptions`, body)
/** The nodes a share on `storage` would attach, and those it would skip with why: the create's own rules,
 *  writing nothing. */
export type ShareTargets = { targets: { node_id: string; name: string }[]; skipped: SkippedNode[] }
export const shareTargets = (storage: NodeLinkStorageBody) =>
  post<ShareTargets>(`${NODELINKS_PATH}/share-subscriptions/targets`, { storage })
/** The router-state key a share hands its `skipped` to the NodeLink page under. */
export const SHARE_SKIPPED_STATE = 'shareSkipped'

/** Delete, or Retry one already `deleting`: `null` once gone (204), else the row still `deleting`. */
export const deleteNodeLink = (id: string) =>
  del<NodeLinkDetail | null>(`${NODELINKS_PATH}/${encodeURIComponent(id)}`).then(row => row ?? null)
/** Delete without waiting for nodes still holding the mount. */
export const deleteNodeLinkAnyway = (id: string) =>
  post<NodeLinkDetail | null>(`${NODELINKS_PATH}/${encodeURIComponent(id)}/delete-anyway`, {}).then(row => row ?? null)

/** Make exactly `nodeIds` mount this NodeLink; each converges live. */
export const setNodeLinkNodes = (id: string, nodeIds: string[]) =>
  put<NodeLinkDetail>(`${NODELINKS_PATH}/${encodeURIComponent(id)}/nodes`, { node_ids: nodeIds })

/* ── One node's NodeLinks (`/api/nodes/{node}/nodelinks`) ── */
/** `not_set_up`: the node has never reported at all (a host still being set up). `needs_permission`: a Mac's share
 *  is mounted but macOS blocks it until Full Disk Access is granted (`mount_reason` names the grant). */
export type NodeMount = 'mounted' | 'not_mounted' | 'needs_permission' | 'unknown' | 'not_set_up'

/** One attached node's mount of one NodeLink, for the whole org in ONE read (`GET …/nodelinks/mounts`). */
export type NodeLinkMount = {
  nodelink_id: string
  node_id: string
  /** null: the node row is gone. */
  node_name: string | null
  mount: NodeMount
  mount_reason: string | null
}

export const listNodeLinkMounts = (orgId?: string) => get<NodeLinkMount[]>(`${NODELINKS_PATH}/mounts${orgQuery(orgId)}`)

export type NodeNodeLink = {
  id: string
  name: string
  slug: string
  mount_path: string
  /** Where it lands on this node; null when this node cannot resolve it. */
  node_mount_path: string | null
  status: NodeLinkStatus
  attached: boolean
  mount: NodeMount
  mount_reason: string | null
}

const nodePath = (nodeId: string) => `/api/nodes/${encodeURIComponent(nodeId)}/nodelinks`

export const getNodeNodeLinks = (nodeId: string) => get<NodeNodeLink[]>(nodePath(nodeId))
/** Make the node mount exactly `nodelinkIds`. */
export const setNodeNodeLinks = (nodeId: string, nodelinkIds: string[]) =>
  put<NodeNodeLink[]>(nodePath(nodeId), { nodelink_ids: nodelinkIds })
