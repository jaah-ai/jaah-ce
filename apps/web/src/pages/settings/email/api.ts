/* An org's own email: the console's client for the API's org-email routes. Every rule is the
 * server's (`app/tenant_gmail/`); these mirror its wire shapes field for field. No read ever carries a secret: a
 * connection says only that one is stored. */

import { del, get, post } from '../../../api'

export type GmailConnectionKind = 'oauth' | 'service_account'

/** `GmailConnectionOut`. */
export type GmailConnection = {
  id: string
  kind: GmailConnectionKind
  address: string
  /** Receiving needs read access: a service account only. */
  can_receive: boolean
  secret_set: boolean
  last_used_at: string | null
  /** Why the latest send failed; null after one worked. */
  last_error: string | null
  /** When receiving last read the mailbox (a service account); null before the first read. */
  last_sync_at: string | null
  /** Why the latest receive sync failed, or what it skipped; null after one worked. */
  sync_error: string | null
  created_at: string
  created_by: string | null
  updated_at: string
}

/** `TenantAddressOut`: a hosted address the org receives at. No automation: its mail lands in Unmatched. */
export type TenantAddress = {
  id: string
  address: string
  /** The label; may equal the address. */
  name: string
  trigger_id: string | null
  automation_id: string | null
  automation_name: string | null
  created_at: string
  created_by: string | null
}

/** `OrgEmailOut`. Off the plan the connections and addresses still list (and remove); nothing new is made.
 *  `mail_domain` "" = this deployment receives no mail, so no address can be created. */
export type OrgEmail = {
  on_plan: boolean
  oauth_available: boolean
  connections: GmailConnection[]
  addresses: TenantAddress[]
  mail_domain: string
}

/** `TenantEmailSendOut`: one sent message, or a refused attempt. */
export type EmailSend = {
  id: string
  created_at: string
  connection_id: string | null
  from_address: string
  to: string[]
  cc: string[]
  subject: string
  status: 'sent' | 'failed'
  error: string | null
  gmail_message_id: string | null
  task_id: number | null
  /** The sending task's title; null when no task sent it or it is gone. */
  task_title: string | null
  /** A schedule run's id / an event run's (its delivery's). */
  automation_run_id: string | null
  trigger_delivery_id: number | null
  /** The run's automation; null when no automation sent it or the run is gone. */
  automation_id: string | null
  automation_name: string | null
  /** The run id the automation's run page opens (a preflight's child delivery opens as its parent). */
  automation_run_page_id: string | null
  /** Who the send is attributed to; null for a system actor. */
  actor: string | null
}

/** Where a send came from, as a console link: its task, or its automation's run. Null when neither (a person) or
 *  the source is gone. */
export function sendSource(send: EmailSend): { label: string; title: string; to: string; verb: string } | null {
  if (send.task_id !== null && send.task_title !== null) {
    return { label: `Task #${send.task_id}`, title: `Sent by task #${send.task_id}: ${send.task_title}`, to: `/tasks/${send.task_id}`, verb: 'Open task' }
  }
  const runId = send.automation_run_page_id
  if (runId !== null && send.automation_id !== null) {
    const name = send.automation_name ?? 'Automation'
    return {
      label: name,
      title: `Sent by a run of automation ${name}`,
      to: `/automations/${encodeURIComponent(send.automation_id)}/runs/${encodeURIComponent(runId)}`,
      verb: 'Open run',
    }
  }
  return null
}

export const KIND_LABEL: Record<GmailConnectionKind, string> = {
  oauth: 'Google sign-in',
  service_account: 'Service account',
}

export const ORG_EMAIL_KEY = ['org-email'] as const
/** The Email card; the OAuth callback's failures land here too (`?gmail_connect=error&reason=`). */
export const EMAIL_SETTINGS_URL = '/settings?area=organizations&section=email'
/** The Email card's Addresses tab (`EmailSection.tsx`'s `?mail=` strip). */
export const EMAIL_ADDRESSES_URL = `${EMAIL_SETTINGS_URL}&mail=addresses`
export const gmailConnectionUrl = (id: string) => `/settings/email/${encodeURIComponent(id)}`

const base = (orgId: string) => `/api/orgs/${encodeURIComponent(orgId)}/email`
const one = (orgId: string, id: string) => `${base(orgId)}/connections/${encodeURIComponent(id)}`

/** The sends read's path (`useListPageQuery` reads it): newest first, optionally one connection's. */
export function sendsPath(orgId: string, params: { connectionId?: string; page: number; per: number }): string {
  const query = new URLSearchParams({ page: String(params.page), per: String(params.per) })
  if (params.connectionId) query.set('connection_id', params.connectionId)
  return `${base(orgId)}/sends?${query}`
}

export const getOrgEmail = (orgId: string) => get<OrgEmail>(base(orgId))
export const getGmailConnection = (orgId: string, id: string) => get<GmailConnection>(one(orgId, id))
export const deleteGmailConnection = (orgId: string, id: string) => del(one(orgId, id))
export const connectServiceAccount = (orgId: string, body: { address: string; key_json: string }) =>
  post<GmailConnection>(`${base(orgId)}/connections/service-account`, body)
export const createTenantAddress = (orgId: string, name: string) =>
  post<TenantAddress>(`${base(orgId)}/addresses`, { name })
export const revokeTenantAddress = (orgId: string, addressId: string) =>
  del(`${base(orgId)}/addresses/${encodeURIComponent(addressId)}`)
/** Connect with Google: the consent URL for the signed-in member's own Gmail, for their org. */
export const startGmailConnect = () => post<{ authorize_url: string }>('/api/email-connect/gmail/start')
