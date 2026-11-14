import type { JSX } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../auth'
import Breadcrumb from '../components/Breadcrumb'
import GitConnectionsCard from '../components/GitConnectionsCard'
import { useNestedReturn } from '../hooks/useNestedCreate'
import { DetailPage } from '../ui/detail/DetailPage'
import { PROFILE_GIT_PATH } from '../ui/RepoUrlLookup'

/* `/profile/git/new` — Profile > Git's Add form (`components/GitConnectionsCard`, `addOnly`) as its own page, so a
 * form that needs a Git account opens it beside itself (the nested-create seam, `hooks/useNestedCreate.ts`; the New
 * project page's Connect) and comes back with its input restored. Connect runs in a popup, so the page is never
 * reloaded. Once an account lands it goes back with `?created=` (the account's credential, or `connected` when a
 * popup Connect stored it server-side); Cancel goes back with nothing. Self-service, unguarded as Profile. */
export default function GitAccountNewPage(): JSX.Element {
  const { admin } = useAuth()
  const navigate = useNavigate()
  const back = useNestedReturn()
  const leave = (created: string | null) => {
    if (back) back(created)
    else navigate(PROFILE_GIT_PATH)
  }
  return (
    <DetailPage
      breadcrumb={<Breadcrumb items={[{ label: 'Profile', to: PROFILE_GIT_PATH }, { label: 'New Git account' }]} />}
      title="New Git account"
      headerTestId="git-account-new-header"
      measure="read"
    >
      {admin && (
        <GitConnectionsCard
          userId={admin.id}
          ownGitCredentials={[]}
          addOnly
          onCancel={() => leave(null)}
          onChanged={next => leave(next?.credential?.id ?? 'connected')}
        />
      )}
    </DetailPage>
  )
}
