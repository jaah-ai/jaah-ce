/* A link that opens the Add node dialog on arrival: `/nodes` honours `?add=1` for an org admin (`pages/Nodes.tsx`)
   and drops it from the URL once read, so Back and a reload do not reopen the dialog. `&org=<id>` opens it on that
   org (the dialog's `initialOrgId`, honoured for the operator only), so a tenant's account lands on its own org. */
export const ADD_NODE_PARAM = 'add'
export const ADD_NODE_ORG_PARAM = 'org'
export const NODES_ADD_PATH = `/nodes?${ADD_NODE_PARAM}=1`

export function nodesAddPath(orgId?: string | null): string {
  return orgId ? `${NODES_ADD_PATH}&${ADD_NODE_ORG_PARAM}=${encodeURIComponent(orgId)}` : NODES_ADD_PATH
}
