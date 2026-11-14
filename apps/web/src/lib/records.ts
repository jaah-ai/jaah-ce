/* THE ONE plain-object guard. A JSON payload, a stored preference block, a trigger config: each is read field by
 * field only once it is known to be an object and not an array (`typeof [] === 'object'`). Do not re-add a local copy. */
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
