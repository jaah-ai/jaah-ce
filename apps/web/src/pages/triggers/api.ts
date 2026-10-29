// What the Triggers API still serves the console that `pages/automations/api.ts` does not: the preflight's state
// document (keyed by the event automation's trigger row, `automation.legacy.id`) and the unmatched inbox.

import { get, put } from '../../api'
import { UNFILTERED_READ } from '../../lib/orgScope'

const BASE = '/api/triggers'
const one = (id: string) => `${BASE}/${encodeURIComponent(id)}`

/** Answers the trigger row; only its `state_document` is read here. */
export const putTriggerState = (id: string, document: Record<string, unknown>) =>
  put<{ state_document?: Record<string, unknown> }>(`${one(id)}/state`, document)

/** `routes/triggers.py::UnmatchedOut` — a Slack mention or operator email no trigger took. Operator only. */
type UnmatchedMessage = {
  id: number
  kind: string
  received_at: string | null
  reason: string | null
  /** An email's sender and subject; null for a Slack mention. */
  sender: string | null
  subject: string | null
  /** Why SES mail was refused before routing (`spam`/`virus`/`dmarc`/`rate_limited`); null = no trigger matched. */
  rejected_reason: string | null
}

/** Past the operator's org filter (`UNFILTERED_READ`): the rows are the caller's own org's (org zero's for the
 *  operator), so a filter naming other orgs would hide every one of them while mail waits. */
export const listUnmatched = (kind: 'gmail' | 'slack', limit = 20) =>
  get<UnmatchedMessage[]>(`${BASE}/unmatched?kind=${kind}&limit=${limit}`, UNFILTERED_READ)
