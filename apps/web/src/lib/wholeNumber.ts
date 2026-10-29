/* THE ONE parser for a typed count or cap (a quantity, a project count, a plan limit): the trimmed text as a
 * whole number in `min`..`max`, else null — empty, signed, fractional, non-numeric and out of range alike.
 * A caller maps null to its own "invalid" (an error message, NaN, undefined); three local copies drifted before. */
export function wholeIn(text: string, min: number, max: number): number | null {
  const t = text.trim()
  if (!/^\d+$/.test(t)) return null
  const n = Number(t)
  return n >= min && n <= max ? n : null
}
