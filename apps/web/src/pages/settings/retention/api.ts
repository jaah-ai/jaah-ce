// An org's own retention windows (`GET/PUT /api/orgs/{org_id}/retention`, rules in `apps/api/app/org_retention.py`):
// how many days its session transcripts, activity events and task artifacts are kept. `days: null` = the platform's
// window. Org Owner/Admin or an operator; a tenant's read answers 403 while the plan's `org_retention_enabled` is off.

import { get, orgPath, put } from '../../../api'

export type RetentionClass = 'transcripts' | 'events' | 'artifacts'

export type RetentionWindow = {
  /** The org's own window held inside the current range; null = `default_days`. */
  days: number | null
  /** The stored value when the range has since moved past it (`days` is then its clamp); else null. */
  clamped_from: number | null
  /** The platform's window: what unset means. */
  default_days: number
  min_days: number
  max_days: number
}

export type Retention = Record<RetentionClass, RetentionWindow>

/** What a save sends: all three, null for the platform default. */
export type RetentionInput = Record<RetentionClass, number | null>

/** Every org's retention read: a save of the operator's bounds re-reads them all (`settings/KnobSection`). */
export const RETENTION_QUERY_ROOT = ['org-retention'] as const
export const retentionKey = (orgId: string) => [...RETENTION_QUERY_ROOT, orgId] as const

const path = (orgId: string) => orgPath(orgId, 'retention')

export const getRetention = (orgId: string) => get<Retention>(path(orgId))
/** 422 with a `detail` sentence naming the range when a value falls outside it. */
export const putRetention = (orgId: string, body: RetentionInput) => put<Retention>(path(orgId), body)
