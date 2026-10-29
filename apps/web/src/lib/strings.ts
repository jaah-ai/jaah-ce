// Small string helpers shared by forms and URL readers. One home each; do not re-add a local copy.

/** Trimmed, or `null` (never `undefined`) when blank: an Edit PUT sends every field, so a cleared field is
 *  cleared server-side rather than dropped by `exclude_unset`. */
export function strOrNull(v: string): string | null {
  const t = v.trim()
  return t ? t : null
}

/** Drops `''` and repeats, first occurrence kept. A URL reader's list filter: ids and names are data, not
 *  validated against a roster that may not be fetched yet, and a round trip must never grow the URL. */
export function uniqueNonEmpty(values: readonly string[]): string[] {
  return [...new Set(values.filter(value => value !== ''))]
}

/** First `count` CODEPOINTS. `Array.from`, never `slice` (UTF-16 units would split an astral char into a
 *  lone surrogate). The initials rules (`lib/authorBadge`, `ui/credentialMarks`) build on it. */
export function firstCodepoints(text: string, count: number): string {
  return Array.from(text).slice(0, count).join('')
}
