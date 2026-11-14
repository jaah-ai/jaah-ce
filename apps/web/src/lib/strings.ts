// Small string helpers shared by forms and URL readers. One home each; do not re-add a local copy.

/** Trimmed, or `null` (never `undefined`) when blank: an Edit PUT sends every field, so a cleared field is
 *  cleared server-side rather than dropped by `exclude_unset`. */
export function strOrNull(v: string): string | null {
  const t = v.trim()
  return t ? t : null
}

/** `text` split on `sep`, each item trimmed, empties dropped: a comma-typed list (tags, env var names, ids)
 *  as the list it names. */
export function splitList(text: string, sep = ','): string[] {
  return text.split(sep).map(item => item.trim()).filter(item => item !== '')
}

/** Drops `''` and repeats, first occurrence kept. A URL reader's list filter: ids and names are data, not
 *  validated against a roster that may not be fetched yet, and a round trip must never grow the URL. */
export function uniqueNonEmpty(values: readonly string[]): string[] {
  return [...new Set(values.filter(value => value !== ''))]
}

/** `raw` when it is one of `vocab`, typed as such, else `null` (callers `?? default`). THE URL-value parser: a
 *  stale or hand-typed link falls back rather than sending a value that 422s. Exact match, never folded. */
export function oneOf<T extends string>(vocab: readonly T[], raw: string | null | undefined): T | null {
  return raw != null && (vocab as readonly string[]).includes(raw) ? (raw as T) : null
}

/** First `count` CODEPOINTS. `Array.from`, never `slice` (UTF-16 units would split an astral char into a
 *  lone surrogate). `initials` builds on it. */
export function firstCodepoints(text: string, count: number): string {
  return Array.from(text).slice(0, count).join('')
}

/** THE initials rule (`lib/authorBadge`'s author badge, `ui/credentialMarks`' monogram): two+ words → the
 *  first and LAST words' first letters ("Ada B. Lovelace" → `AL`); one word → its first two (`System` must not
 *  read as a truncated `S`). Capped at two AFTER upper-casing: a ligature upper-cases into two chars (`'ﬁnance'`
 *  → `'FI'` + `'N'`). `''` for blank text. */
export function initials(text: string): string {
  const words = text.trim().split(/\s+/).filter(word => word !== '')
  if (words.length === 0) return ''
  const letters =
    words.length === 1
      ? firstCodepoints(words[0], 2)
      : `${firstCodepoints(words[0], 1)}${firstCodepoints(words[words.length - 1], 1)}`
  return firstCodepoints(letters.toUpperCase(), 2)
}
