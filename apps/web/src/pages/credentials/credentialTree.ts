/* THE CREDENTIAL ROSTER'S NAME-TREE VOCABULARY: what `lib/nameTree.ts`, `lib/nameTreeMove.ts` and
 * `lib/nameTreeRows.tsx` need to know about a credential (its noun, name cap, DOM handles), and the key of a folder
 * in the persisted expansion set. Tree mode itself is `./CredentialTreeView.tsx`, loaded a level at a time from the
 * server. */
import type { NameTreeKind } from '../../lib/nameTree'

/** Mirrors `routes/credentials/credential_wire.py::_NAME_MAX_LEN` (measured trimmed). */
export const CREDENTIAL_NAME_MAX = 200

export const CREDENTIAL_TREE_KIND: NameTreeKind = {
  noun: { one: 'secret', many: 'secrets' },
  nameMax: CREDENTIAL_NAME_MAX,
  idPrefix: 'credential',
}

/* ── EXPANSION PER HOME ──
 * The persisted expansion set (`view.expanded`) holds folder keys; two homes may both hold an `aws` folder, so a
 * folder's key is its home (`CredentialHomeNode.key`), a unit separator, then its path. */
const HOME_KEY_SEPARATOR = '\u001f'

/** `aws` in home `o:<org id>` → `o:<org id>␟aws`; the home's own top is the key with an empty path. */
export function homeExpansionKey(home: string, path: string): string {
  return `${home}${HOME_KEY_SEPARATOR}${path}`
}
