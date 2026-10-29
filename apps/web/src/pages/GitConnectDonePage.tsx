import { useEffect, type JSX } from 'react'
import { Navigate, useSearchParams } from 'react-router-dom'
import Typography from '@mui/material/Typography'
import type { GitConnectDoneMessage } from '../components/projects/githubOAuthPopup'

/** Where the create wizard's GitHub popup lands (`githubOAuthPopup.ts`): tell the opener how the connect
 *  ended, then close. Opened any other way, it is Profile's Git tab with the same answer. */
export default function GitConnectDonePage(): JSX.Element {
  const [params] = useSearchParams()
  const opener = window.opener as Window | null
  useEffect(() => {
    if (!opener) return
    const message: GitConnectDoneMessage = {
      type: 'jaah-git-connect', result: params.get('git_connect') ?? '', reason: params.get('reason') ?? '',
    }
    opener.postMessage(message, window.location.origin)
    window.close()
  }, [opener, params])
  if (!opener) return <Navigate to={`/profile?tab=git-accounts&${params.toString()}`} replace />
  return <Typography sx={{ p: 2 }}>Done. You can close this window.</Typography>
}
