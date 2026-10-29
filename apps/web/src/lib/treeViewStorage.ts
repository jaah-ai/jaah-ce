/* The two VIEW fields every Flat/Tree roster persists — the mode and its open folders — read back from
 * storage. Shared by `credentialFilterStorage.ts` and `listViewStorage.ts`, so both rosters
 * widen a bad payload the same way: an unknown mode is Flat, a bad expansion set is empty. Pure. */

import { validEntries } from './safeStorage'

/** Flat or Tree (`name` split on `/`, `lib/nameTree.ts`). FLAT IS THE DEFAULT: it hides nothing behind a
 *  disclosure. */
export type TreeViewMode = 'flat' | 'tree'

/** Closed vocabulary: an unknown `mode` would render neither list. */
export function readTreeMode(value: unknown): TreeViewMode {
  return value === 'tree' ? 'tree' : 'flat'
}

/** Ceiling on the never-pruned expansion set, so a corrupted or crafted payload cannot make every render
 *  allocate a huge `Set`. 512 is far above any real session; the TAIL is dropped (widening fallback). Not a
 *  security control. */
const MAX_EXPANDED_PATHS = 512

/** An expansion set, or `[]` for a non-array. Members are checked too (a stored number or `null` would sit
 *  in the `Set` forever, unremovable) and filtered, not rejected wholesale; `''` is dropped (no path is
 *  empty). */
export function readExpandedPaths(value: unknown): string[] {
  return validEntries(value, (entry): entry is string => typeof entry === 'string' && entry !== '').slice(0, MAX_EXPANDED_PATHS)
}
