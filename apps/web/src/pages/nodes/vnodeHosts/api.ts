// API client for `routes/vnode_hosts.py` (hosts). Superadmin-only — every org's hosts for the
// operator, the caller's own otherwise; never folded into `nodes`. Deletes use the host's own routes, not
// `DELETE /api/nodes/{name}`: that route is org-scoped, and a co-located node may sit in another org than
// its host, so it can 404 on both.

import { del, get, patch } from '../../../api'
import type { NodeDeleteResult, VirtualNode, VirtualNodeCapsPatch, VnodeHost } from '../../../types'

export const VNODE_HOSTS_KEY = ['vnode-hosts'] as const
export const virtualNodesKey = (hostNodeId: string) => ['vnode-hosts', hostNodeId, 'virtual-nodes'] as const

export const listVnodeHosts = () => get<VnodeHost[]>('/api/vnode-hosts')

export const listVirtualNodes = (hostNodeId: string) =>
  get<VirtualNode[]>(`/api/vnode-hosts/${encodeURIComponent(hostNodeId)}/virtual-nodes`)

/** Sets or clears a co-located node's disk/inode cap overrides. Operator only (a tenant gets 403). */
export const updateVirtualNodeCaps = (hostNodeId: string, virtualNodeId: string, body: VirtualNodeCapsPatch) =>
  patch<VirtualNode>(
    `/api/vnode-hosts/${encodeURIComponent(hostNodeId)}/virtual-nodes/${encodeURIComponent(virtualNodeId)}`,
    body,
  )

export const deleteVirtualNode = (hostNodeId: string, virtualNodeId: string) =>
  del<NodeDeleteResult>(
    `/api/vnode-hosts/${encodeURIComponent(hostNodeId)}/virtual-nodes/${encodeURIComponent(virtualNodeId)}`,
  )

/** `uninstall`: a self-hosted machine is also told to uninstall Jaah (a cloud one is terminated regardless). */
export const deleteVnodeHost = (hostNodeId: string, opts?: { uninstall?: boolean }) =>
  del<NodeDeleteResult>(`/api/vnode-hosts/${encodeURIComponent(hostNodeId)}${opts?.uninstall ? '?uninstall=true' : ''}`)

/** Offer an operator host to the Shared hosting pool, up to `sharedCapacity` nodes (1–99); null stops
 *  offering it. Operator-only; answers the updated host row. */
export const setSharedCapacity = (hostNodeId: string, sharedCapacity: number | null) =>
  patch<VnodeHost>(`/api/vnode-hosts/${encodeURIComponent(hostNodeId)}`, { shared_capacity: sharedCapacity })
