import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useAuth } from '../../../auth'
import { isOrgAdminSeat } from '../../../lib/orgAdmin'
import { useEventStream } from '../../../hooks/useEventStream'
import { useLiveRefetch } from '../../../hooks/useLiveRefetch'
import type { VnodeHost } from '../../../types'
import { listVnodeHosts, VNODE_HOSTS_KEY } from './api'

/** What the roster needs to know about hosts: the hosts themselves (in the server's order) and which node
 *  uuids ARE hosts, keyed on `nodes.id`. */
export type VnodeHostIndex = {
  list: VnodeHost[]
  hosts: Map<string, VnodeHost>
  loading: boolean
  refetch: () => Promise<unknown>
}

/* ONLY FOR AN ORG ADMIN, and not fetched at all otherwise — `GET /api/vnode-hosts` is `@org_admin`
   (the operator reads every org's hosts, a tenant's Org Owner/Admin its own org's), so the query runs for
   exactly the readers it admits. Refetched on any `node` event: a host launch, and a co-located node
   created or deleted, each emit one. */
export function useVnodeHosts(): VnodeHostIndex {
  const { admin } = useAuth()
  const enabled = isOrgAdminSeat(admin)
  const query = useQuery({ queryKey: VNODE_HOSTS_KEY, queryFn: listVnodeHosts, enabled })
  useLiveRefetch(useEventStream(), () => { if (enabled) void query.refetch() }, null, { entity: 'node' })

  const data = enabled ? query.data : undefined
  return useMemo(() => {
    const list = data ?? []
    const hosts = new Map<string, VnodeHost>()
    for (const host of list) hosts.set(host.host_node_id, host)
    return {
      list,
      hosts,
      loading: enabled && query.isLoading,
      refetch: () => (enabled ? query.refetch() : Promise.resolve()),
    }
  }, [data, enabled, query.isLoading])
}
