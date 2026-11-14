/** First letter up, the rest as given: `webhook` → "Webhook". */
export function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1)
}

/** A server token's underscores as spaces, case kept: `branch_protection` → "branch protection" (mid-sentence). */
export function words(token: string): string {
  return token.replace(/_/g, ' ')
}

/** A server token as words: `identity_denied` → "Identity denied". Filters and `data-*` keep the raw token. */
export function humanize(token: string): string {
  const text = words(token).trim()
  return text ? capitalize(text) : token
}

/* The one place a `NumberFormat` is built ('en-US' is the console's one locale): `usdFormat` for the console's own
   figures (costs, spend), `currencyFormat` for a tenant's money in its own `code` (an unknown code throws). */
export const numberFormat = (options: Intl.NumberFormatOptions = {}): Intl.NumberFormat => new Intl.NumberFormat('en-US', options)
export const currencyFormat = (code: string, options: Intl.NumberFormatOptions = {}): Intl.NumberFormat =>
  numberFormat({ style: 'currency', currency: code, ...options })
export const usdFormat = (options?: Intl.NumberFormatOptions): Intl.NumberFormat => currencyFormat('USD', options)

const COMPACT = numberFormat({ notation: 'compact', maximumFractionDigits: 1 })
const GROUPED = numberFormat({ maximumFractionDigits: 0 })

/** A whole figure with its thousands grouped (`8,134`): quotas, token and message counts read exactly. */
export function groupedCount(n: number): string {
  return GROUPED.format(n)
}

/** A figure kept short: whole below a thousand ("856"), else one decimal of K/M/B ("12.4K", "3.1M", "1.2B"),
 *  trailing ".0" dropped. Token counts, event estimates, tile readings. */
export function compactCount(n: number): string {
  return COMPACT.format(n)
}

/** A share as a whole percent: `0.256` → "26%". A rate, a survival, a read rate; the caller words its absence. */
export function percent(share: number): string {
  return `${Math.round(share * 100)}%`
}

/** A figure whole when whole, else one decimal: `2`, `0.5`, `12.3`. A runner count's mean, a comparison's
 *  median and quartiles. Not `compactCount`: no K/M, the figure is small and read exactly. */
export function oneDecimal(n: number): string {
  return Number.isInteger(n) ? String(n) : n.toFixed(1)
}

/** `1 org` / `2 orgs`: a count with its noun, plural by `s` unless `many` spells it (`enquiries`). */
export function countOf(n: number, noun: string, many = `${noun}s`): string {
  return `${n} ${n === 1 ? noun : many}`
}

/** `countOf` with the figure grouped en-US (`8,134 tokens`): quotas, token, row and message counts. */
export function groupedCountOf(n: number, noun: string, many = `${noun}s`): string {
  return `${groupedCount(n)} ${n === 1 ? noun : many}`
}
