/* THE ONE string hash: FNV-1a, 32-bit, over UTF-16 code units, as an unsigned integer. Stable across sessions
 * (a chart keeps a group's hue, an org keeps its tint) and well spread even over ids that differ only in their last
 * few chars (UUIDv7 shares a timestamp prefix), which a weak mixer collapses. Do not re-add a local copy. */
export function fnv1a(text: string): number {
  let hash = 0x811c9dc5
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i)
    hash = Math.imul(hash, 0x01000193)
  }
  return hash >>> 0
}
