/* Re-filing a named row by dragging: the rule for the new `name`, and whether the drop is allowed — for
 * every roster `lib/nameTree.ts` draws (Credentials, Automation > Schedules).
 * A rename, not a move: folders are a view over `name`, so the only write is a PATCH of `name` (one row)
 * or the roster's own folder-move route (a folder). A credential's `org_id` is not writable
 * (`routes/credentials/credential_wire.py::_DECLARED_BODY_FIELDS`), so a drag cannot cross tenants; the org rule is
 * about what the tree would claim. Pure: no React, hook or DOM.
 */
import { NAME_PATH_SEPARATOR, nameSegments, type NamedRow, type NameTreeKind } from './nameTree'

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

/** A drop the feed will take (with the name it would write), or a refusal with the sentence shown on
 *  the row. No third answer: a target that silently does nothing is what this prevents. */
export type NameDropVerdict =
  | { ok: true; name: string }
  | { ok: false; reason: string }

/** The two refusals whose words name the roster's own rows. */
const collisionReason = (kind: NameTreeKind) => `A ${kind.noun.one} of that name is already here`
const tenantReason = (kind: NameTreeKind) => `Holds another organization’s ${kind.noun.many}`

/**
 * May this row be dropped here, including "only within one organization".
 * The org arm is defence in depth, not enforcement: the read seam binds `principal.org_id`
 * (`tenancy/seam.py`) and a credential's `org_id` is not updatable. It gives a correct refusal if the
 * seam runs in shadow, and keeps the tree from drawing a folder that mixes tenants. A roster whose rows
 * carry no `org_id` reads every row as "no org", so the arm never refuses there.
 * `destOrgIds` is the set of distinct `org_id`s (with `null` a value) among the destination's held
 * descendants; a drop is allowed only when it holds nothing but the dragged row's org.
 * A partial roster cannot defeat it: a group exists only because a loaded leaf sits under it, so the set
 * is never empty; a hidden other-org sibling means the folder was already mixed. An empty set would pass
 * vacuously, so `nameTreeRows.tsx`'s `dropDrag` refuses an absent subtree.
 * The top level is always open (`destPath === null`): it is no folder, and testing the whole roster's
 * orgs would make dragging out of a folder a one-way door.
 */
export function nameDropVerdict(
  kind: NameTreeKind,
  row: NamedRow,
  destPath: string | null,
  destOrgIds: ReadonlySet<string | null>,
  destLeafNames: ReadonlySet<string>,
): NameDropVerdict {
  const name = movedName(row.name, destPath)
  // The no-folder name, from the same function; the two are null together.
  const leaf = movedName(row.name, null)
  if (name === null || leaf === null) {
    return { ok: false, reason: 'This name has no segment to re-file' }
  }
  /* "Already here" compares folders, not strings: `a / b` renders under `a`, but `movedName` yields
  `a/b`, and a no-op rename still changes the key runners resolve by. */
  if (nameParentPath(row.name) === (destPath ?? '')) return { ok: false, reason: 'Already here' }
  /* A collision is a refusal: runners resolve credentials by `name`, and with no unique constraint two
  rows sharing one make `get_task_credential` 409 for both (a schedule has no such reader, but two
  identically-named siblings could only be told apart by opening them). The top level is the worst
  case, since it strips the whole prefix. */
  if (destLeafNames.has(leaf)) return { ok: false, reason: collisionReason(kind) }
  if (kind.nameMax !== null && name.length > kind.nameMax) {
    return { ok: false, reason: `The name would exceed ${kind.nameMax} characters` }
  }
  if (destPath !== null) {
    const org = row.org_id ?? null
    for (const held of destOrgIds) {
      if (held !== org) return { ok: false, reason: tenantReason(kind) }
    }
  }
  return { ok: true, name }
}

/* ── Re-filing a whole FOLDER ─────────────────────────────────────────────────────────────────────────
 * Still a rename, of every row beneath the folder, done server-side in one transaction (the bulk move,
 * `routes/credentials/bulk.py`, through `writes.py::rename_credentials`) so a refusal part-way never splits the
 * folder. The rules below mirror the server's so the gesture refuses up front; the server still decides. */

/** A folder in flight: its path, and what the verdict needs from beneath it, gathered at lift. */
export type NameFolderDrag = {
  path: string
  /** The rows beneath it — what the server is told the operator saw, and moves exactly. */
  ids: readonly string[]
  /** Every leaf name beneath the folder, normalised (`a / b` → `a/b`). */
  names: readonly string[]
  /** Every distinct `org_id` beneath it, `null` a value. */
  orgs: ReadonlySet<string | null>
}

/** A leaf's name after its folder moves: `a/b/x` with folder `a/b` into `c` is `c/b/x`. The folder keeps
 *  its own last segment; `destPath === null` is the top level. */
export function movedFolderLeafName(name: string, folderPath: string, destPath: string | null): string {
  const folder = folderPath.split(NAME_PATH_SEPARATOR)
  const rest = nameSegments(name).slice(folder.length)
  const head = destPath ? [destPath, folder[folder.length - 1]] : [folder[folder.length - 1]]
  return [...head, ...rest].join(NAME_PATH_SEPARATOR)
}

/**
 * May this folder be dropped here? `allNames` is every loaded leaf name, normalised — the collision test
 * is exact (a renamed name equal to a row staying put), not a sibling-name guess, because a folder
 * merging into a same-named folder is legal until two names actually meet.
 * The org rule is the leaf's: into a folder, the union of both sides' orgs must be one org.
 */
export function folderDropVerdict(
  kind: NameTreeKind,
  folder: NameFolderDrag,
  destPath: string | null,
  destOrgIds: ReadonlySet<string | null>,
  allNames: ReadonlySet<string>,
): NameDropVerdict {
  const dest = destPath ?? ''
  if (dest === folder.path || dest.startsWith(`${folder.path}${NAME_PATH_SEPARATOR}`)) {
    return { ok: false, reason: 'Cannot move a folder into itself' }
  }
  if (nameParentPath(folder.path) === dest) return { ok: false, reason: 'Already here' }
  const movingNames = new Set(folder.names)
  for (const name of folder.names) {
    const moved = movedFolderLeafName(name, folder.path, destPath)
    if (kind.nameMax !== null && moved.length > kind.nameMax) {
      return { ok: false, reason: `A name would exceed ${kind.nameMax} characters` }
    }
    /* The folder's own leaves are moving too, so meeting one of them is not a collision (the server's
       `staying` set excludes them the same way). */
    if (allNames.has(moved) && !movingNames.has(moved)) return { ok: false, reason: collisionReason(kind) }
  }
  if (destPath !== null && new Set([...folder.orgs, ...destOrgIds]).size > 1) {
    return { ok: false, reason: tenantReason(kind) }
  }
  return { ok: true, name: movedFolderLeafName(folder.path, folder.path, destPath) }
}
