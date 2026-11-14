import Box from '@mui/material/Box'
import type { ReactNode } from 'react'
import { get } from '../../api'
import { readAsOnce } from '../../lib/actAs'
import type { CredentialView } from '../../lib/credentialFilterStorage'
import type { OrgRef } from '../../types'
import Banner from '../../ui/Banner'
import { listCredentials, PICKER_PAGE, type Credential, type CredentialList } from './api'

// THE LAST-GITHUB-SECRET GUARDRAIL: a delete confirm that would leave the viewer no `github` secret (the token
// runners clone, push and open PRs with) says so before Delete. Counted over what the viewer can see, so it is
// advice, never a gate: a failed count shows no warning and the delete still runs. A bulk set of more than
// `PICKER_PAGE` visible GitHub secrets is not checked.

const GITHUB_TYPE = 'github'

export const LAST_GITHUB_WARNING =
  'Deleting the only set of GitHub credentials will prevent Jaah from implementing any new functionality.'

/** The confirm's body: `message`, with the warning above it when `warn`. */
export function withLastGithubWarning(message: ReactNode, warn: boolean): ReactNode {
  if (!warn) return message
  return (
    <>
      <Box sx={{ mb: 1.5 }}>
        <Banner severity="warning" testId="last-github-warning">{LAST_GITHUB_WARNING}</Banner>
      </Box>
      {message}
    </>
  )
}

async function githubTotal(filters: Partial<CredentialView> = {}): Promise<number> {
  const { total } = await listCredentials(1, 1, {
    type: GITHUB_TYPE,
    q: filters.q,
    mine: filters.mine,
    ownerIds: filters.ownerIds,
    secrets: filters.secrets,
    health: filters.health,
  })
  return total
}

/** Is `row` the viewer's last GitHub secret? An operator's row of another org (`org`) counts in that org, as
 *  its Delete goes as that org. */
export async function deletesLastGithub(row: Credential, org: OrgRef | null): Promise<boolean> {
  if (row.type !== GITHUB_TYPE) return false
  try {
    const { total } = await get<CredentialList>(`/api/credentials?page=1&per=1&type=${GITHUB_TYPE}`, ...readAsOnce(org))
    return total <= 1
  } catch {
    return false
  }
}

/** Would a bulk delete remove every GitHub secret the viewer can see? `ids` for ticked rows, `applied` for
 *  "all matching" (its filters, re-asked with the type pinned to GitHub). `shown` is the page on screen: ticks
 *  all on it, none GitHub, need no request. */
export async function bulkDeletesLastGithub(
  set: { ids: readonly string[] } | { applied: CredentialView },
  shown: readonly Credential[],
): Promise<boolean> {
  if ('ids' in set) {
    const types = new Map(shown.map(row => [row.id, row.type]))
    if (set.ids.every(id => types.has(id) && types.get(id) !== GITHUB_TYPE)) return false
  }
  try {
    if ('applied' in set) {
      if (set.applied.type && set.applied.type !== GITHUB_TYPE) return false
      const all = await githubTotal()
      return all > 0 && (await githubTotal(set.applied)) === all
    }
    const { items, total } = await listCredentials(1, PICKER_PAGE, { type: GITHUB_TYPE })
    if (total === 0 || total > items.length) return false
    const ticked = new Set(set.ids)
    return items.every(row => ticked.has(row.id))
  } catch {
    return false
  }
}
