/* THE LIVE TOTAL (R15a): computed here, from the catalogue already on screen, on every render — never fetched,
 * so a change of type, storage or tier shows its price in the same frame. Display only: the server prices the
 * order from its own table on submit (Architecture §5). */
import { moneyText } from '../../lib/billing'
import type { HostingCatalog } from './api'

export type HostingKind = 'dedicated' | 'shared'

/** A Dedicated machine's OS: Ubuntu, or Windows on a type the catalogue prices a Windows license for. */
export type HostingOs = 'ubuntu' | 'windows'

export type HostingChoice = {
  kind: HostingKind
  instanceType: string
  storageGb: number | null
  tierKey: string
  /** Dedicated only; absent = Ubuntu. */
  os?: HostingOs
}

/** What the choice costs per month, in cents. Null when the choice is not complete or not priced. */
export type HostingTotal = number | null

export function hostingTotal(catalog: HostingCatalog, choice: HostingChoice): HostingTotal {
  if (choice.kind === 'shared') {
    const tier = catalog.shared_tiers.find(t => t.key === choice.tierKey)
    return tier ? tier.monthly_cents : null
  }
  const type = catalog.instance_types.find(t => t.key === choice.instanceType)
  if (!type?.available) return null
  const storage = catalog.storage.find(s => s.gb === choice.storageGb)
  if (!storage) return null
  if (choice.os !== 'windows') return type.monthly_cents + storage.monthly_cents
  // No license price = Windows is not sold on this type.
  if (type.windows_monthly_cents == null) return null
  return type.monthly_cents + storage.monthly_cents + type.windows_monthly_cents
}

/** "$12.00/mo", or "Free" for 0. */
export function priceText(cents: number, currency: string): string {
  return cents === 0 ? 'Free' : `${moneyText(cents, currency)}/mo`
}

/** "2 vCPU · 4 GB RAM", leaving out what the catalogue does not know. */
export function sizeText(vcpu: number | null, ramGb: number | null): string {
  const parts: string[] = []
  if (vcpu != null) parts.push(`${vcpu} vCPU`)
  if (ramGb != null) parts.push(`${Number.isInteger(ramGb) ? ramGb : ramGb.toFixed(1)} GB RAM`)
  return parts.join(' · ')
}
