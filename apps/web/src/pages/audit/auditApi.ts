/* The Activity tab's by-id read — one audit row, in the browse list's item shape (`AuditRow`). The list's own fetch
 * stays in `UsersTab.tsx`. */

import { get } from '../../api'
import type { AuditRow } from './types'

/** One row of the trail by id — a pasted row URL's load. 404 for a row outside the caller's org or never served. */
export const getAuditEvent = (eventId: number) => get<AuditRow>(`/api/audit/events/${eventId}`)
