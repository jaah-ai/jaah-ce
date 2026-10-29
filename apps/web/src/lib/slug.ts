/* THE ONE SLUG DERIVATION: lower-case, each run of non-alphanumerics one `-`, cut to `limit`, no `-` at
 * either end. Empty when nothing survives (the caller names its default). A DERIVATION the server repeats
 * (`apps/api/app/entity_shape.py::slugify` is its twin), never a validation: each surface's grammar is its
 * own route's. */
export function slugFrom(text: string, limit?: number): string {
  const base = text.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')
  return limit === undefined ? base : base.slice(0, limit).replace(/-+$/, '')
}
