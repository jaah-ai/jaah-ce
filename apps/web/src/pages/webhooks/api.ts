import { del, get, patch, post, withQuery } from '../../api'

/* OUTGOING WEBHOOKS ON THE WIRE — `apps/api/app/routes/webhooks.py`, field for field. Every route is `@org_admin`;
 * a tenant whose plan leaves `webhooks_enabled` off is refused 403 `{code: "plan"}` on every write. The secret is
 * answered ONCE, by the create and by rotate-secret; no read serves it again. Server vocabularies stay `string`: a
 * state or event type this console does not know yet renders as itself. */

export type WebhookLastDelivery = { state: string; status_code: number | null; at: string | null }

export type Webhook = {
  id: string
  slug: string
  name: string
  description: string
  url: string
  event_types: string[]
  /** null: every project. */
  project_ids: number[] | null
  enabled: boolean
  secret_set: boolean
  secret_rotated_at: string | null
  created_by: string | null
  created_at: string | null
  updated_by: string | null
  updated_at: string | null
  last_delivery: WebhookLastDelivery | null
}

/** The create's and rotate-secret's answer: the endpoint plus its secret, this once. */
export type WebhookWithSecret = Webhook & { secret: string }

export type WebhookBody = {
  name: string
  description: string
  url: string
  event_types: string[]
  project_ids: number[] | null
  enabled: boolean
}

export type WebhookEventType = { type: string; label: string; description: string }

export type WebhookDelivery = {
  id: string
  event_id: string | number | null
  event_type: string
  state: string
  attempts: number
  next_attempt_at: string | null
  last_status_code: number | null
  last_latency_ms: number | null
  last_error: string | null
  created_at: string | null
  payload: unknown
}

export type DeliveryPage = { items: WebhookDelivery[]; next_cursor: string | null }

export const WEBHOOKS_PATH = '/integrations/webhooks'
export const NEW_WEBHOOK_PATH = `${WEBHOOKS_PATH}/new`
export const webhookPath = (id: string) => `${WEBHOOKS_PATH}/${encodeURIComponent(id)}`

export const WEBHOOKS_KEY = ['webhooks'] as const
export const webhookKey = (id: string) => ['webhook', id] as const
export const EVENT_TYPES_KEY = ['webhook-event-types'] as const

const base = (id: string) => `/api/webhooks/${encodeURIComponent(id)}`

export const listWebhooks = () => get<{ items: Webhook[] }>('/api/webhooks').then(r => r.items ?? [])
export const getWebhook = (id: string) => get<Webhook>(base(id))
export const listWebhookEventTypes = () => get<WebhookEventType[]>('/api/webhooks/event-types')
export const createWebhook = (body: WebhookBody) => post<WebhookWithSecret>('/api/webhooks', body)
export const updateWebhook = (id: string, body: Partial<WebhookBody>) => patch<Webhook>(base(id), body)
export const deleteWebhook = (id: string) => del(base(id))
export const rotateWebhookSecret = (id: string) => post<WebhookWithSecret>(`${base(id)}/rotate-secret`)
export const testWebhook = (id: string) => post<WebhookDelivery>(`${base(id)}/test`)
export const listWebhookDeliveries = (id: string, cursor: string | null, limit = 50) =>
  get<DeliveryPage>(withQuery(`${base(id)}/deliveries`, { limit, cursor }))
export const redeliverWebhookDelivery = (id: string, deliveryId: string) =>
  post<WebhookDelivery>(`${base(id)}/deliveries/${encodeURIComponent(deliveryId)}/redeliver`)

/** A delivery state as the console names it; an unknown one as itself. */
export const DELIVERY_STATE: Record<string, { label: string; tone: 'success' | 'warning' | 'error' }> = {
  pending: { label: 'Retrying', tone: 'warning' },
  succeeded: { label: 'Delivered', tone: 'success' },
  failed: { label: 'Failed', tone: 'error' },
}

/** A pending delivery that has not been tried yet is queued, not retrying. */
export function deliveryState(d: { state: string; attempts?: number }): { label: string; tone: 'success' | 'warning' | 'error' | 'neutral' } {
  if (d.state === 'pending' && (d.attempts ?? 0) === 0) return { label: 'Queued', tone: 'neutral' }
  return DELIVERY_STATE[d.state] ?? { label: d.state, tone: 'neutral' }
}
