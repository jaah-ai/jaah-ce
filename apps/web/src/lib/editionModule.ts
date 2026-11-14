/* AN EDITION-GATED OPTIONAL MODULE: the one way a console file loads a surface some edition lacks (a feature row of
 * packages/editions/features.toml, or `routesCloud.tsx`'s cloud-only tree). The file is read by `import.meta.glob`,
 * never imported: an edition whose tree leaves it out compiles with no stub, because the glob yields `{}` for an absent
 * file. The glob stays a literal at the call site — Vite resolves it statically, so it can neither move here nor take a
 * variable path — and is passed as a thunk so an off feature never runs a lazy glob (an eager one is a static import
 * Vite hoists wherever it appears, as the old ternary's was). Import-free (edition.ts's rule). */

/** The one export of an edition-gated module, or undefined: when the feature is off, and when the edition's tree
 *  leaves the file out (`{}`). Pass `import.meta.glob('<one literal path>', …)` as the thunk, `{ eager: true }` for the
 *  module itself (or `import: '<name>'` for one export), eager-less for a lazy loader. */
export function editionModule<T>(on: boolean, glob: () => Record<string, T>): T | undefined {
  return on ? Object.values(glob())[0] : undefined
}
