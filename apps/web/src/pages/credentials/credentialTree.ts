/* THE CREDENTIAL ROSTER'S TREE VOCABULARY: the key of a folder in the persisted expansion set. Tree mode itself
 * is `./CredentialTreeView.tsx`, loaded a level at a time from the server. */

/* ── EXPANSION PER HOME ──
 * The persisted expansion set (`view.expanded`) holds folder keys; two homes may both hold an `aws` folder, so a
 * folder's key is its home (`CredentialHomeNode.key`), a unit separator, then its path. */
const HOME_KEY_SEPARATOR = '\u001f'

/** `aws` in home `o:<org id>` → `o:<org id>␟aws`; the home's own top is the key with an empty path. */
export function homeExpansionKey(home: string, path: string): string {
  return `${home}${HOME_KEY_SEPARATOR}${path}`
}
