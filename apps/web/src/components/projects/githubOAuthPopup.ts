import { safeHref } from '../../lib/safeHref'
import { startForgeConnect } from '../../pages/credentials/api'
import { queryClient } from '../../queryClient'
import type { GitRepoOwners } from './NewRepoFields'

/* Connect GitHub through the OAuth App in a popup, for an owner the GitHub App may not create in: its
 * token creates with the person's own rights, and is kept as their GitHub connection (`routes/git_connect.py`). The
 * popup lands on `/git-connect/done` (`GitConnectDonePage`), which posts `GitConnectDoneMessage` here and closes. */

/** The owners' cache key (`NewRepoFields::useRepoOwners`): read again after a connect, which ends the hand-off. */
export const REPO_OWNERS_KEY = 'git-repo-owners'

/** What `/git-connect/done` posts to its opener: the callback's `?git_connect=` and `?reason=`. */
export type GitConnectDoneMessage = { type: 'jaah-git-connect'; result: string; reason: string }

/* The callback's slugs a person can act on here (`routes/git_connect.py::_ERRORS`); any other names itself. */
const REASONS: Record<string, string> = Object.assign(Object.create(null), {
  denied: 'GitHub approval was declined. Press Create to try again.',
  expired: 'GitHub approval took over ten minutes. Press Create to try again.',
})

/** Open the popup. Call it before any `await`: a popup opened later in a click's handler is blocked. */
export function openConnectPopup(): Window | null {
  return window.open('', 'jaah-github-connect', 'popup,width=640,height=760')
}

/** Drive `popup` through GitHub's approval; resolves once connected, throws a sentence otherwise. */
export async function connectInPopup(popup: Window | null): Promise<void> {
  if (!popup) throw new Error('The browser blocked the GitHub window. Allow pop-ups for this site, then press Create again.')
  let href: string | undefined
  try {
    href = safeHref((await startForgeConnect('github', '/git-connect/done', 'oauth_app')).authorize_url)
  } catch (err) {
    popup.close()
    throw err
  }
  if (!href) {
    popup.close()
    throw new Error('The GitHub authorization link the server returned is not a usable URL.')
  }
  popup.location.href = href
  const done = await new Promise<GitConnectDoneMessage | null>(resolve => {
    const finish = (value: GitConnectDoneMessage | null) => {
      window.removeEventListener('message', onMessage)
      window.clearInterval(closed)
      resolve(value)
    }
    const onMessage = (e: MessageEvent) => {
      const data = e.data as Partial<GitConnectDoneMessage> | null
      if (e.origin !== window.location.origin || e.source !== popup || data?.type !== 'jaah-git-connect') return
      finish({ type: data.type, result: String(data.result ?? ''), reason: String(data.reason ?? '') })
    }
    window.addEventListener('message', onMessage)
    // A window closed by the person posts nothing: polling `closed` is the only way to hear it.
    const closed = window.setInterval(() => { if (popup.closed) finish(null) }, 500)
  })
  if (!done) throw new Error('The GitHub window was closed before approval. Press Create to try again.')
  if (done.result !== 'connected') {
    throw new Error(REASONS[done.reason] ?? `GitHub could not be connected (${done.reason || 'unknown error'}).`)
  }
  /* The token is the OAuth App's now, so no owner is a hand-off: cleared in the cache at once, or a row's sync from a
  read that is still stale (or a refetch that fails) would send a retried Create back through the popup. */
  queryClient.setQueriesData<GitRepoOwners>({ queryKey: [REPO_OWNERS_KEY] }, old => old && {
    ...old, oauth_connect: false, owners: old.owners.map(o => ({ ...o, create_url: null })),
  })
  await queryClient.invalidateQueries({ queryKey: [REPO_OWNERS_KEY] })
}
