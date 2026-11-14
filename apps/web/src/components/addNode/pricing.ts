/* A HOSTING CHOICE AND ITS WORDS, shared by the hosting picker (bundle lines, Change product). Display only: the
 * server prices from its own table (Architecture §5). */
import { moneyText } from '../../lib/billing'

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
