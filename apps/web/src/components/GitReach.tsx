import Button from '@mui/material/Button'
import Link from '@mui/material/Link'
import { useQuery } from '@tanstack/react-query'
import type { JSX } from 'react'
import { get } from '../api'
import Banner from '../ui/Banner'

/* WHETHER THE VIEWER'S OWN GIT CREDENTIALS CAN WRITE TO A PROJECT'S REPOSITORY — `GET /api/projects/{id}/git-reach`
   (`routes/projects.py::ProjectGitReachOut`), the answer a task they start is served by (`app/git_reach.py`).
   `blocked` warns: no credential of theirs can push or open a PR there, with what is missing and where to fix
   it. `fallback` names the credential their tasks push as instead of their selected connection. `ok`/`unknown`
   say nothing. */
export type ProjectGitReach = {
  state: 'ok' | 'fallback' | 'blocked' | 'unknown'
  credential: string | null
  problem: string | null
  fix_url: string | null
}

export const gitReachPath = (projectId: number) => `/api/projects/${projectId}/git-reach`

/** The `blocked` answer's message, or null for every other state. */
export function blockedReach(reach: ProjectGitReach | undefined | null): ProjectGitReach | null {
  return reach?.state === 'blocked' && reach.problem ? reach : null
}

/** The project page's preflight: a warning above the tabs when no credential of the viewer's can write. */
export function GitReachBanner({ projectId }: { projectId: number }): JSX.Element | null {
  const query = useQuery({
    queryKey: ['project-git-reach', projectId],
    queryFn: () => get<ProjectGitReach>(gitReachPath(projectId)),
    // A courtesy read: a failure renders nothing, and GitHub is not asked again on every focus.
    retry: false,
    staleTime: 60_000,
  })
  const blocked = blockedReach(query.data)
  if (query.data?.state === 'fallback' && query.data.credential) {
    return (
      <Banner severity="info" testId="project-git-reach">
        Tasks push as <strong>{query.data.credential}</strong>: your selected GitHub connection can't write here.
      </Banner>
    )
  }
  if (!blocked) return null
  return (
    <Banner
      severity="warning"
      testId="project-git-reach"
      action={blocked.fix_url ? (
        <Button color="inherit" size="small" href={blocked.fix_url} target="_blank" rel="noopener noreferrer">
          Fix
        </Button>
      ) : undefined}
    >
      <strong>Tasks can't push here.</strong> {blocked.problem}
    </Banner>
  )
}

/** The same message for Start's toast: the problem, then a link to fix it. */
export function GitReachText({ reach }: { reach: ProjectGitReach }): JSX.Element {
  return (
    <span data-testid="start-git-reach">
      <strong>This task can't push.</strong> {reach.problem}{' '}
      {reach.fix_url && (
        <Link href={reach.fix_url} target="_blank" rel="noopener noreferrer" color="inherit" underline="always">
          Fix
        </Link>
      )}
    </span>
  )
}
