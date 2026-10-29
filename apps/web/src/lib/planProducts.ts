/* A plan's bundled products (`routes/billing_plans.py::ProductIn/ProductOut`, PRD D-5): a Shared node of a tier,
 * a Dedicated node of a size and storage, Jaah AI credit a month — each with a quantity — or Live Cast: its
 * quantity is projects, with a Backfill (how many of a project's latest tasks are evaluated when published). Shared by the plan
 * editor (Products › Plans) and an org's products override. The catalogue they are picked from is the
 * tenant hosting catalogue's own shape (`GET /api/billing/plans/product-catalog`), so a plan bundles exactly what
 * a tenant can order. */
import { get } from '../api'
import type { HostingCatalog } from '../components/addNode/api'
import { priceText } from '../components/addNode/pricing'
import { priceTagText } from './billing'

export type ProductKind = 'shared_node' | 'dedicated_node' | 'jaah_ai_credit' | 'live_cast'

export const PRODUCT_KIND_LABEL: Record<ProductKind, string> = {
  shared_node: 'Shared node',
  dedicated_node: 'Dedicated node',
  jaah_ai_credit: 'Jaah AI',
  live_cast: 'Live Cast',
}

/** The server's refusal of a Live Cast line while it has no price (`catalog.live_cast.offered` false). */
export const LIVE_CAST_NOT_OFFERED = "Live Cast isn't offered. Set its price in Products › Live Cast."

/** "Live Cast · 3 projects · Backfill 100" — `app/plan_terms.py::describe`'s wording; never "N ×". Without
 *  `backfill`, the head alone ("Live Cast · 1 project"). */
export function liveCastLabel(projects: number, backfill?: number): string {
  const head = `Live Cast · ${projects} ${projects === 1 ? 'project' : 'projects'}`
  return backfill === undefined ? head : `${head} · Backfill ${backfill}`
}

/** What a write sends: only the fields of its `kind` matter. */
export type ProductLineIn = {
  kind: ProductKind
  quantity: number
  tier_key?: string | null
  instance_type?: string | null
  storage_gb?: number | null
  credit_cents?: number | null
  /** Live Cast only (1..1000). */
  backfill_tasks?: number | null
}

/** What a read returns: the line plus the server's own `label` ("2 × Shared node · Tier S"). */
export type ProductLine = ProductLineIn & {
  id?: string | null
  label?: string
  fingerprint?: string | null
}

export const PRODUCT_CATALOG_PATH = '/api/billing/plans/product-catalog'

/** Operator-only: what a plan (or an org's override) may bundle. */
export const getProductCatalog = () => get<HostingCatalog>(PRODUCT_CATALOG_PATH)

/** A label without the count, from the catalogue's names; `app/plan_terms.py::describe`'s wording. */
function baseLabel(line: ProductLineIn, catalog: HostingCatalog | undefined): string {
  if (line.kind === 'shared_node') {
    const tier = catalog?.shared_tiers.find(t => t.key === line.tier_key)
    const disk = tier?.disk_gb != null ? ` · ${tier.disk_gb} GB` : ''
    const name = tier?.name ?? line.tier_key ?? ''
    // A tier named for its kind ("Shared (8 vCPU, 32 GB)") says Shared once, as `describe` writes it.
    const named = name.slice(0, 6).toLowerCase() === 'shared' && !/^[\p{L}\p{N}]/u.test(name.slice(6, 7))
    // "Shared node S" keeps one "node"; no name (nor key) is the bare kind, never "Shared node · ".
    let rest = name.slice(6)
    if (named && rest.slice(0, 5).toLowerCase() === ' node' && !/^[\p{L}\p{N}]/u.test(rest.slice(5, 6))) rest = rest.slice(5)
    return `${named ? `Shared node${rest}` : name ? `Shared node · ${name}` : 'Shared node'}${disk}`
  }
  if (line.kind === 'dedicated_node') {
    const type = catalog?.instance_types.find(t => t.key === line.instance_type)
    return `Dedicated node · ${type?.name ?? line.instance_type ?? ''} · ${line.storage_gb ?? '?'} GB`
  }
  // "$5" / "$5.50", as `app/plan_terms.py::_dollars` writes it, so a local label reads as the server's.
  return `Jaah AI · ${priceTagText(line.credit_cents ?? 0, 'usd')}/month`
}

/** The line's readable label: the server's when it sent one, else the same wording built from the catalogue. */
export function productLabel(line: ProductLine, catalog?: HostingCatalog): string {
  if (line.label) return line.label
  if (line.kind === 'live_cast') return liveCastLabel(line.quantity, line.backfill_tasks ?? undefined)
  const label = baseLabel(line, catalog)
  return line.quantity === 1 ? label : `${line.quantity} × ${label}`
}

/** The write body for `line`: the fields of its kind only. */
export function productIn(line: ProductLine): ProductLineIn {
  if (line.kind === 'shared_node') return { kind: line.kind, quantity: line.quantity, tier_key: line.tier_key }
  if (line.kind === 'dedicated_node') {
    return { kind: line.kind, quantity: line.quantity, instance_type: line.instance_type, storage_gb: line.storage_gb }
  }
  if (line.kind === 'live_cast') return { kind: line.kind, quantity: line.quantity, backfill_tasks: line.backfill_tasks }
  return { kind: line.kind, quantity: line.quantity, credit_cents: line.credit_cents ?? 0 }
}

/** Same products in the same order — a write is needed only when this is false. */
export function sameProducts(a: readonly ProductLine[], b: readonly ProductLine[]): boolean {
  return JSON.stringify(a.map(productIn)) === JSON.stringify(b.map(productIn))
}

/** A Dedicated fingerprint's monthly catalogue price ("dedicated_node:<type>:<gb>"), or null when unknown. */
export function dedicatedMonthly(fingerprint: string, catalog: HostingCatalog | undefined): string | null {
  const [kind, ...rest] = fingerprint.split(':')
  if (kind !== 'dedicated_node' || !catalog) return null
  const gb = Number(rest.pop())
  const type = catalog.instance_types.find(t => t.key === rest.join(':'))
  const storage = catalog.storage.find(s => s.gb === gb)
  if (!type) return null
  return priceText(type.monthly_cents + (storage?.monthly_cents ?? 0), catalog.currency)
}
