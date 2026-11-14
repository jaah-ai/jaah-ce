/* A plan's bundled product lines as the plan and org-plan reads serve them (`routes/billing_plans.py::ProductOut`):
 * shown as text, never edited here (Products › Plans and an org's products override edit them through the product
 * engine's line editor, `lib/products.ts`). The hosting catalogue a plan's dry run prices its Dedicated nodes from is
 * the tenant hosting catalogue's own shape (`GET /api/billing/plans/product-catalog`). */
import { get } from '../api'
import type { HostingCatalog } from '../components/addNode/api'
import { priceText } from '../components/addNode/pricing'

/** One line as read: the server's readable `label` ("2 × Shared node · Tier S") and its fields. */
export type ProductLine = {
  id?: string | null
  /** A product kind key (`GET /api/products/kinds`). */
  kind: string
  quantity: number
  tier_key?: string | null
  instance_type?: string | null
  storage_gb?: number | null
  credit_cents?: number | null
  audience?: 'all' | 'existing' | 'new'
  audience_at?: string | null
  label?: string
  fingerprint?: string | null
}

export const PRODUCT_CATALOG_PATH = '/api/billing/plans/product-catalog'

/** Operator-only: what a plan (or an org's override) may bundle. */
export const getProductCatalog = () => get<HostingCatalog>(PRODUCT_CATALOG_PATH)

/** A Dedicated fingerprint's monthly catalogue price ("<kind>:<type>:<gb>"), or null when it names no instance type. */
export function dedicatedMonthly(fingerprint: string, catalog: HostingCatalog | undefined): string | null {
  const [, ...rest] = fingerprint.split(':')
  if (!catalog || rest.length < 2) return null
  const gb = Number(rest.pop())
  const type = catalog.instance_types.find(t => t.key === rest.join(':'))
  const storage = catalog.storage.find(s => s.gb === gb)
  if (!type) return null
  return priceText(type.monthly_cents + (storage?.monthly_cents ?? 0), catalog.currency)
}
