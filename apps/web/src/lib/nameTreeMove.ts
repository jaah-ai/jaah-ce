/* A named row's place in the tree, and its name after a re-file — for the roster that re-files by dragging
 * (`pages/credentials/CredentialTreeView.tsx`). A rename, not a move: folders are a view over `name`, so the
 * only write is a PATCH of `name`. Pure: no React, hook or DOM.
 */
import { NAME_PATH_SEPARATOR, nameSegments, type NamedRow } from './nameTree'

/** The group path a row sits in — `'a/b'` for `a/b/c`, `''` for a `/`-free or separators-only name.
 *  From normalised segments (`a // b /c` → `a/b`), comparable with a group's `path`. */
export function nameParentPath(name: string): string {
  return nameSegments(name).slice(0, -1).join(NAME_PATH_SEPARATOR)
}

/**
 * The row's name after the drag: its last segment under `destPath`, or top level for `null`.
 * `null` for a separators-only name: re-hanging the raw name would normalise back to a root leaf, making
 * the row vanish from the folder it was dropped in. Such a row is un-draggable instead.
 */
export function movedName(name: string, destPath: string | null): string | null {
  const segments = nameSegments(name)
  if (segments.length === 0) return null
  const leaf = segments[segments.length - 1]
  return destPath ? `${destPath}${NAME_PATH_SEPARATOR}${leaf}` : leaf
}

/** False only for the segment-less name above. */
export function isNameDraggable(row: NamedRow): boolean {
  return nameSegments(row.name).length > 0
}
