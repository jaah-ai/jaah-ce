// The system-mail delivery log, suppression list and DMARC reports (`routes/email_delivery_log.py`). Every route
// is operator-only (`require_operator`), and every read goes past the operator's org filter (`UNFILTERED_READ`):
// the rows are org zero's.

import { del, get, post } from '../../../api'
import { UNFILTERED_READ } from '../../../lib/orgScope'

export type DeliveryStatus =
  | 'queued' | 'sent' | 'delivered' | 'delayed' | 'bounced' | 'complained' | 'suppressed' | 'failed' | 'not_configured'

export type DeliveryEvent = { type: string; at: string; detail: string | null }

export type EmailDelivery = {
  id: string
  created_at: string
  recipient: string
  mail_type: string
  subject: string
  status: DeliveryStatus
  status_detail: string | null
  attempts: number
  sent_at: string | null
  last_event_at: string | null
  events: DeliveryEvent[]
}

export type DeliveryPage = { items: EmailDelivery[]; next_before: string | null }

export type EmailSuppression = {
  address: string
  reason: 'bounce' | 'complaint' | 'manual'
  detail: string | null
  created_at: string
  source_delivery_id: string | null
}

/** Newest first; `before` is the previous page's `next_before`. */
export function listDeliveries(filters: { recipient?: string; status?: string; before?: string | null }) {
  const query = new URLSearchParams()
  if (filters.recipient) query.set('recipient', filters.recipient)
  if (filters.status) query.set('status', filters.status)
  if (filters.before) query.set('before', filters.before)
  const qs = query.toString()
  return get<DeliveryPage>(`/api/operator/email/deliveries${qs ? `?${qs}` : ''}`, UNFILTERED_READ)
}

export const listSuppressions = () => get<EmailSuppression[]>('/api/operator/email/suppressions', UNFILTERED_READ)

/** Sendable again, in Jaah and on SES's account list. */
export const clearSuppression = (address: string) =>
  del(`/api/operator/email/suppressions/${encodeURIComponent(address)}`)

type DmarcCounts = { messages: number; dkim_pass: number; spf_pass: number; dmarc_pass: number }

export type DmarcSource = DmarcCounts & {
  source_ip: string
  /** The authenticated domains that reported it. */
  reporters: string[]
  header_from: string[]
  last_seen: string
}

/** `reporter_domain` is who SES authenticated the reports as; `reporter_org` the name they gave themselves. */
export type DmarcReporter = DmarcCounts & {
  reporter_domain: string
  reporter_org: string
  reports: number
  last_report: string
}

/** Sources failing DMARC come first (`app/dmarc.py::summary`). */
export type DmarcSummary = DmarcCounts & {
  days: number
  reports: number
  /** Every failing source in the window, not only the listed ones. */
  failing_sources: number
  sources: DmarcSource[]
  reporters: DmarcReporter[]
}

export const getDmarcSummary = (days: number) =>
  get<DmarcSummary>(`/api/operator/email/dmarc?days=${days}`, UNFILTERED_READ)

/** The SES account's sandbox state (`GET /api/operator/email/ses`). Every SES field is null when system mail is not
 *  configured, or when SES refused the read (`error`). `gmail_fallback`: a Gmail connection would carry a send SES
 *  refuses. */
export type SesAccount = {
  configured: boolean
  production_access: boolean | null
  review_status: string | null
  case_id: string | null
  max_24h: number | null
  sent_24h: number | null
  gmail_fallback: boolean
  error: string | null
}

export type SesRecipientStatus = 'verified' | 'pending' | 'failed' | 'none'
export type SesRecipient = { address: string; status: SesRecipientStatus }

export const SES_ACCOUNT_QUERY = ['ses-account'] as const
/** Settings › Integrations › SES account (`SesAccountSection.tsx`). */
export const SES_ACCOUNT_URL = '/settings?area=integrations&section=ses-account'

export const getSesAccount = () => get<SesAccount>('/api/operator/email/ses', UNFILTERED_READ)

/** Asks AWS to lift the sandbox; answers the account as it now reads. */
export const requestProductionAccess = (body: { website_url: string; use_case_description: string }) =>
  post<SesAccount>('/api/operator/email/ses/production-access', body)

/** SES emails `address` a verification link (a fresh one when an earlier link was not opened). */
export const verifySesRecipient = (address: string) =>
  post<SesRecipient>('/api/operator/email/ses/recipients', { address })
