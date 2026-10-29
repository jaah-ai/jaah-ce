// An org's data region (`GET/PUT /api/orgs/{org_id}/data-region`, rules in `apps/api/app/data_residency.py`):
// where its transcripts and task artifacts are stored. Org Owner/Admin or an operator; a tenant's read answers 403
// while the plan's `data_residency_enabled` is off.

import { get, orgPath, put } from '../../../api'
import type { RegionOption } from '../../../lib/dataRegions'

export type Residency = {
  /** The org's choice; null = never chosen (its data lives in the control-plane region). */
  region: string | null
  effective_region: string
  effective_label: string
  /** The regions it may choose, the control plane's first. */
  regions: RegionOption[]
  /** What always stays in the control-plane region, one sentence. */
  stays: string
  /** What residency does not cover, one sentence. */
  not_covered: string
  /** Why the region can no longer change (data already lives in it), else null. */
  locked: string | null
}

export const residencyKey = (orgId: string) => ['org-data-region', orgId] as const

const path = (orgId: string) => orgPath(orgId, 'data-region')

export const getResidency = (orgId: string) => get<Residency>(path(orgId))
/** 409 with a `detail` sentence when data already lives in another region; 422 for an unknown region. */
export const putResidency = (orgId: string, region: string) => put<Residency>(path(orgId), { region })
