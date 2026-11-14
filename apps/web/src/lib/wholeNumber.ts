/* THE ONE parser for a typed count or cap (a quantity, a project count, a plan limit): the trimmed text as a
 * whole number, else null — empty, signed, fractional and non-numeric alike. `wholeIn` adds a range. A caller
 * maps null to its own "invalid" (an error message, NaN, undefined); local copies drifted before. */
export function wholeNumber(text: string): number | null {
  const t = text.trim()
  return /^\d+$/.test(t) ? Number(t) : null
}

/** `wholeNumber`, and within `min`..`max` inclusive; out of range is null too. */
export function wholeIn(text: string, min: number, max: number): number | null {
  const n = wholeNumber(text)
  return n !== null && n >= min && n <= max ? n : null
}
