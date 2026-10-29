// An org's IP allowlist (`GET/PUT /api/orgs/{org_id}/ip-ranges`): the networks its members may reach the
// console from. Empty = any address. Nodes and runners are never blocked. Org Owner/Admin or an operator;
// a tenant's read answers 403 while the plan's `ip_allowlist_enabled` switch is off. React-free.

import { get, orgPath, put } from '../api'

export type IpRange = {
  id: string
  /** Normalized by the server: host bits cleared, a bare address stored as /32 or /128. */
  cidr: string
  label: string
  updated_at: string
  updated_by: string | null
  /** The changer's email, shown on hover; null where the reader may not see that person. */
  updated_by_email: string | null
}

/** What a save sends: the whole list, replacing the stored one. */
export type IpRangeInput = { cidr: string; label: string }

/** The list and the caller's own address as the server sees it (null when it cannot tell). */
export type IpAllowlist = { ranges: IpRange[]; your_ip: string | null }

/** An IPv4 or IPv6 address's characters, nothing else: an address a refusal names is shown only in this shape. */
export const IP_SHAPE = /^[0-9A-Fa-f:.]{2,45}$/

export const ipAllowlistKey = (orgId: string) => ['ip-allowlist', orgId] as const

const path = (orgId: string) => orgPath(orgId, 'ip-ranges')

export const getIpRanges = (orgId: string) => get<IpAllowlist>(path(orgId))
/** Replaces the list; `[]` turns the allowlist off. 422 with a `detail` sentence on a bad range, a duplicate,
 *  too many ranges, or — for a tenant — a list that leaves out the saver's own address. */
export const putIpRanges = (orgId: string, ranges: IpRangeInput[]) => put<IpAllowlist>(path(orgId), { ranges })

/** Whether the caller's own org enforces an allowlist (`GET /api/auth/me/ip-allowlist`): any signed-in user;
 *  false for an exempt operator. Profile › Security says so. */
export const ipAllowlistStatusKey = ['ip-allowlist', 'me'] as const
export const getIpAllowlistStatus = () => get<{ on: boolean }>('/api/auth/me/ip-allowlist')
