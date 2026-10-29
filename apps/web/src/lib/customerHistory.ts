// A customer's History feed (`GET /api/customers/{org_id}/history`): everything that happened to the org,
// newest first, kept forever, paged by the server's opaque `next` cursor. React-free.

import { get } from '../api'
import type { Tone } from '../ui/StatusChip'

export type HistoryCategory = 'billing' | 'products' | 'plan' | 'access' | 'lifecycle'
export type HistoryMode = 'with_billing' | 'without_billing'

/** The categories in the filter's order, each with its word and its feed mark. */
export const HISTORY_CATEGORIES: readonly { value: HistoryCategory; label: string; glyph: string; tone: Tone }[] = [
  { value: 'billing', label: 'Billing', glyph: '$', tone: 'success' },
  { value: 'products', label: 'Products', glyph: '▣', tone: 'info' },
  { value: 'plan', label: 'Plan', glyph: '◆', tone: 'neutral' },
  { value: 'access', label: 'Access', glyph: '⚿', tone: 'neutral' },
  { value: 'lifecycle', label: 'Lifecycle', glyph: '●', tone: 'neutral' },
]

export const isHistoryCategory = (value: string | null): value is HistoryCategory =>
  HISTORY_CATEGORIES.some(c => c.value === value)

export type HistoryEntry = {
  id: number
  at: string
  category: HistoryCategory
  kind: string
  summary: string
  actor: { kind: 'user' | 'stripe' | 'system'; label: string | null }
  /** Null: the action moves no money either way (n.a.). */
  mode: HistoryMode | null
  refs: Record<string, unknown>
  invoice_url: string | null
}

export type HistoryPage = { items: HistoryEntry[]; next: string | null }

export const HISTORY_PAGE = 50

export function getHistory(orgId: string, category: HistoryCategory | null, before: string | null): Promise<HistoryPage> {
  const params = new URLSearchParams({ limit: String(HISTORY_PAGE) })
  if (category) params.set('category', category)
  if (before) params.set('before', before)
  return get<HistoryPage>(`/api/customers/${encodeURIComponent(orgId)}/history?${params}`)
}

/** Who did it: the person, else "Stripe" / "System". */
export function actorText(actor: HistoryEntry['actor']): string {
  if (actor.kind === 'stripe') return 'Stripe'
  if (actor.kind === 'system') return actor.label || 'System'
  return actor.label || 'Unknown user'
}

export function modeText(mode: HistoryMode | null): string | null {
  if (mode === 'with_billing') return 'With billing'
  if (mode === 'without_billing') return 'Without billing'
  return null
}

/** The product the entry is about, as `?product=` on the Products tab reads it (a hosting's rental id, a
 *  comp's or an included copy's id); null when it names none. */
export function productRef(refs: Record<string, unknown>): string | null {
  for (const key of ['rental_id', 'hosting_id', 'comp_id', 'bundle_id', 'product_id']) {
    const value = refs[key]
    if (typeof value === 'string' && value) return value
  }
  return null
}
