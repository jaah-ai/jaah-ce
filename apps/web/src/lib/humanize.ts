/** First letter up, the rest as given: `webhook` → "Webhook". */
export function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1)
}

/** A server token as words: `identity_denied` → "Identity denied". Filters and `data-*` keep the raw token. */
export function humanize(token: string): string {
  const words = token.replace(/_/g, ' ').trim()
  return words ? capitalize(words) : token
}

const COMPACT = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 })
const GROUPED = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 })

/** A whole figure with its thousands grouped (`8,134`): quotas, token and message counts read exactly. */
export function groupedCount(n: number): string {
  return GROUPED.format(n)
}

/** A figure kept short: whole below a thousand ("856"), else one decimal of K/M/B ("12.4K", "3.1M", "1.2B"),
 *  trailing ".0" dropped. Token counts, event estimates, tile readings. */
export function compactCount(n: number): string {
  return COMPACT.format(n)
}

/** `1 org` / `2 orgs`: a count with its noun, plural by `s` unless `many` spells it (`enquiries`). */
export function countOf(n: number, noun: string, many = `${noun}s`): string {
  return `${n} ${n === 1 ? noun : many}`
}
