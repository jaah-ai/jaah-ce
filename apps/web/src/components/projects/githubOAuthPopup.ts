import { safeHref } from '../../lib/safeHref'
import { startForgeConnect } from '../../pages/credentials/api'
import { queryClient } from '../../queryClient'
import type { GitRepoOwners } from './NewRepoFields'

/* Connect a forge in a popup, so the page that asked is never left: the create wizard's GitHub hand-off owner
 * (`connectInPopup`: through the OAuth App, whose token creates with the person's own rights) and its Repository
 * step's first Git account (`components/projects/RepoGitConnect`, any forge). The popup lands on `/git-connect/done`
 * (`GitConnectDonePage`), which posts `GitConnectDoneMessage` here and closes. */

/** The owners' cache key (`NewRepoFields::useRepoOwners`): read again after a connect, which ends the hand-off. */
export const REPO_OWNERS_KEY = 'git-repo-owners'

/** What `/git-connect/done` posts to its opener: the callback's `?git_connect=` and `?reason=`. */
export type GitConnectDoneMessage = { type: 'jaah-git-connect'; result: string; reason: string }

/* The callback's slugs a person can act on here (`routes/git_connect.py::_ERRORS`); any other names itself. */
const REASONS: Record<string, (label: string, retry: string) => string> = Object.assign(Object.create(null), {
  denied: (l: string, retry: string) => `${l} approval was declined. ${retry}`,
  expired: (l: string, retry: string) => `${l} approval took over ten minutes. ${retry}`,
})

/** Open the popup. Call it before any `await`: a popup opened later in a click's handler is blocked. */
export function openConnectPopup(): Window | null {
  return window.open('', 'jaah-github-connect', 'popup,width=640,height=760')
}

/** Drive `popup` through `forge`'s Connect (`label` names it, `retry` is the sentence saying how to try again);
 *  resolves once connected, throws a sentence otherwise. Every forge's Connect may land on `/git-connect/done`
 *  (`routes/git_connect.py::RETURN_PATHS`), so the page that opened it is never left. */
export async function connectForgeInPopup(
  popup: Window | null, forge: string, label: string, retry: string, app?: 'oauth_app',
): Promise<void> {
  if (!popup) throw new Error(`The browser blocked the ${label} window. Allow pop-ups for this site. ${retry}`)
  let href: string | undefined
  try {
    href = safeHref((await startForgeConnect(forge, '/git-connect/done', app)).authorize_url)
  } catch (err) {
    popup.close()
    throw err
  }
  if (!href) {
    popup.close()
    throw new Error(`The ${label} authorization link the server returned is not a usable URL.`)
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
  if (!done) throw new Error(`The ${label} window was closed before approval. ${retry}`)
  if (done.result !== 'connected') {
    throw new Error(REASONS[done.reason]?.(label, retry) ?? `${label} could not be connected (${done.reason || 'unknown error'}).`)
  }
}

/** Connect GitHub through the OAuth App in `popup` for a hand-off owner; resolves once connected. */
export async function connectInPopup(popup: Window | null): Promise<void> {
  await connectForgeInPopup(popup, 'github', 'GitHub', 'Press Create to try again.', 'oauth_app')
  /* The token is the OAuth App's now, so no owner is a hand-off: cleared in the cache at once, or a row's sync from a
  read that is still stale (or a refetch that fails) would send a retried Create back through the popup. */
  queryClient.setQueriesData<GitRepoOwners>({ queryKey: [REPO_OWNERS_KEY] }, old => old && {
    ...old, oauth_connect: false, owners: old.owners.map(o => ({ ...o, create_url: null })),
  })
  await queryClient.invalidateQueries({ queryKey: [REPO_OWNERS_KEY] })
}
