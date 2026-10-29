// The node-group surface's one API client. It lives under /nodes, not `settings/`, because a group is a
// live fleet object. `settings/cloudConnections/api.ts` stays the home of connections and
// `listCloudConnectionOptions`, which this module deliberately does not re-declare. Record shapes live in
// `src/types.ts`; only the write shape is declared here.

import { del, get, post, put } from '../../../api'
import type { AssignableProject, NodeGroupMarket, NodeGroupRow } from '../../../types'

/* Same body on POST and PUT, and both child lists are FULL REPLACEMENT: the server deletes whatever is
not in `capacity`/`projects`. An absent line means "delete it", so the dialog always submits the whole
set. No `undefined` anywhere: `JSON.stringify` drops it, and a dropped `projects` key would unassign
every project. */
export type NodeGroupWrite = {
  name: string
  description: string
  /** The cloud connection the group launches through; the server derives `credential_id` from it. */
  connection_id: string
  /** True ⇒ the reconcile loop starts EC2 instances for this group on its own timer. */
  enabled: boolean
  /** Unique on `(market, instance_type)` server-side; the dialog refuses a duplicate pair first. */
  capacity: { market: NodeGroupMarket; instance_type: string; desired: number }[]
  /** The projects the group's nodes serve; every project is eligible. */
  projects: { project_id: number }[]
  /** The NodeLinks the group's nodes are born attached to. Unlike the two lists, ABSENT = UNCHANGED, so the
   *  key is left out (never `undefined`) unless picked. `[]` = none. */
  nodelinks?: string[]
}

/* `org_id` is on the POST and not the PUT (boundary B28): a group's tenant is decided once at creation.
No client-side default: the operator's pick on `ui/OrgSelect`; every other superadmin sends their own org
(the seam refuses another). */
export type NodeGroupCreate = NodeGroupWrite & { org_id: string }

export const listNodeGroups = () => get<NodeGroupRow[]>('/api/node-groups')

export const getNodeGroup = (id: string) => get<NodeGroupRow>(`/api/node-groups/${encodeURIComponent(id)}`)

export const createNodeGroup = (body: NodeGroupCreate) =>
  post<NodeGroupRow>('/api/node-groups', body)

/** Full replacement, not a patch. No `org_id` on an update. */
export const updateNodeGroup = (id: string, body: NodeGroupWrite) =>
  put<NodeGroupRow>(`/api/node-groups/${id}`, body)

export const deleteNodeGroup = (id: string) => del(`/api/node-groups/${id}`)

/** The projects a group may target. */
export const listAssignableProjects = () =>
  get<AssignableProject[]>('/api/node-groups/assignable-projects')

/* `CLOUD_CONNECTIONS_PATH` is single-homed in `settings/cloudConnections/api.ts`; not re-exported here. */
