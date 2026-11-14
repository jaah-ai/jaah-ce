import { useEffect, useState, type JSX } from 'react'
import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { useAuth } from '../../auth'
import { gitConnectionsQuery } from '../../pages/credentials/api'

/** The Git account New page (`pages/GitAccountNewPage.tsx`): Profile > Git's Add form as its own page. */
export const GIT_ACCOUNT_NEW_PATH = '/profile/git/new'

/* THE ONE PLACE A GIT CONNECTION IS ASKED FOR: the create wizard's Repository step for an existing app, the first
 * time the viewer has none (no onboarding guide asks — a Jaah-made project needs none). It is one line with
 * Connect; Connect (`onConnect`) opens the Git account New page beside the form through the nested-create seam
 * (`hooks/useNestedCreate.ts`), which comes back with the wizard's input restored. A pasted repository URL still works.
 *
 * Shown from the moment the viewer's connections read none, and kept for the rest of the mount. */

export default function RepoGitConnect({ onConnect }: { onConnect: () => void }): JSX.Element | null {
  const { admin } = useAuth()
  const userId = admin?.id ?? ''
  const query = useQuery({ ...gitConnectionsQuery(userId), enabled: userId !== '' })
  /* Only when nothing is connected at all: an account that needs reconnecting is said once, by the Repo URL field
     (`ui/RepoUrlLookup.tsx`'s couldn't-read line, with its Profile > Git link), never here too. */
  const none = query.data !== undefined && query.data.length > 0 && query.data.every(row => row.state === 'not_connected')
  const [shown, setShown] = useState(false)
  useEffect(() => { if (none) setShown(true) }, [none])
  if (userId === '' || !(shown || none)) return null
  return (
    <Box data-testid="project-repo-git-connect" sx={{ mb: 2 }}>
      <Box sx={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', columnGap: 1 }}>
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          No Git account connected. Connect one to pick a repository, or paste its URL.
        </Typography>
        <Button size="small" onClick={onConnect} data-testid="project-repo-git-connect-open">Connect</Button>
      </Box>
    </Box>
  )
}
