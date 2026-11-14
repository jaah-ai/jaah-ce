import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import ExternalLink from '../ui/ExternalLink'
import { useQuery } from '@tanstack/react-query'
import type { JSX } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import { get } from '../api'
import Banner from '../ui/Banner'
import Tip from '../ui/Tip'
import { usePhone } from '../hooks/usePhone'

/* WHETHER THE VIEWER'S OWN GIT CREDENTIALS CAN WRITE TO A PROJECT'S REPOSITORY — `GET /api/projects/{id}/git-reach`
   (`routes/projects.py::ProjectGitReachOut`), the answer a task they start is served by (`app/git_reach.py`).
   `blocked` warns: no credential of theirs can push or open a PR there, with what is missing and where to fix
   it. `fallback` names the credential their tasks push as instead of their selected connection. `ok`/`unknown`
   say nothing. `clones` lists the nodes whose pool clone cannot read the repository: a clone runs as its
   clone owner, not the viewer (`git_reach.clone_blocks`). Nodes failing for the same owner and reason share ONE
   warning that lists them (one per node filled a phone screen), with a way to fix it. */
export type ProjectGitReach = {
  state: 'ok' | 'fallback' | 'blocked' | 'unknown'
  credential: string | null
  problem: string | null
  fix_url: string | null
  clones?: CloneReach[]
  /** Set when the caller's own login cannot clone the repository: Start offers to run without it. */
  read_problem?: string | null
  read_fix_url?: string | null
}

/** One node whose clone cannot read the repository (`routes/projects.py::CloneReachOut`). */
export type CloneReach = {
  node: string
  node_id: string
  owner: string | null
  owner_id?: string | null
  problem: string
  fix_url: string | null
  /** The Worktrees view narrowed to this node and project, whose slot drawer offers Change clone owner; null
   *  before the node has a slot of the project (the view would list nothing). */
  change_path: string | null
  /** Profile > Git, when the owner is the viewer and has no usable connection: the fix is to connect. */
  connect_path?: string | null
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
  const groups = groupClones(query.data?.clones ?? [])
  if (groups.length === 0) return <ViewerReach reach={query.data} />
  return (
    <>
      <ViewerReach reach={query.data} />
      {groups.map(group => <CloneReachBanner key={group[0].node_id} clones={group} />)}
    </>
  )
}

/** The clones that would say the same thing — same owner, reason and fixes — together, in the server's order. */
function groupClones(clones: CloneReach[]): CloneReach[][] {
  const groups = new Map<string, CloneReach[]>()
  for (const clone of clones) {
    const key = JSON.stringify([clone.owner_id ?? clone.owner, clone.problem, clone.fix_url, clone.connect_path ?? null])
    groups.set(key, [...(groups.get(key) ?? []), clone])
  }
  return [...groups.values()]
}

/** One Worktrees view narrowed to every listed node that has a slot of the project, or null when none has. */
function changePath(clones: CloneReach[]): string | null {
  const paths = clones.map(c => c.change_path).filter((path): path is string => Boolean(path))
  if (paths.length <= 1) return paths[0] ?? null
  const merged = new URLSearchParams(paths[0].split('?')[1])
  for (const path of paths.slice(1)) {
    for (const node of new URLSearchParams(path.split('?')[1]).getAll('node')) merged.append('node', node)
  }
  return `${paths[0].split('?')[0]}?${merged.toString()}`
}

/** Nodes whose clone runs as someone who cannot read the repository: tasks there are refused at checkout. One line
 *  naming the nodes and the clone owner; the server's reason is the hover. The fixes take the banner's side slot,
 *  but under the line on a phone, where the side slot would squeeze both. */
function CloneReachBanner({ clones }: { clones: CloneReach[] }): JSX.Element {
  const [first] = clones
  const change = changePath(clones)
  const phone = usePhone()
  const actions = [
    first.connect_path && (
      <Button key="connect" color="inherit" size="small" component={RouterLink} to={first.connect_path}>
        Connect
      </Button>
    ),
    change && (
      <Button key="change" color="inherit" size="small" component={RouterLink} to={change}>
        Change clone owner
      </Button>
    ),
    first.fix_url && (
      <Button key="fix" color="inherit" size="small" href={first.fix_url} target="_blank" rel="noopener noreferrer">
        Fix
      </Button>
    ),
  ].filter(Boolean)
  const fixes = actions.length
    ? <Box sx={{ display: 'flex', gap: 0.5, flexWrap: 'wrap', ...(phone ? { mt: 0.5, ml: -0.5 } : { justifyContent: 'flex-end' }) }}>{actions}</Box>
    : null
  return (
    <Banner severity="warning" testId="project-clone-reach" action={phone ? undefined : fixes ?? undefined}>
      <Tip title={first.problem}>
        <span data-testid="project-clone-reach-text" aria-description={first.problem} tabIndex={0}>
          Clones on <strong>{clones.map(c => c.node).join(', ')}</strong> fail
          {first.owner ? <> as <strong>{first.owner}</strong></> : null}.
        </span>
      </Tip>
      {phone && fixes}
    </Banner>
  )
}

function ViewerReach({ reach }: { reach: ProjectGitReach | undefined }): JSX.Element | null {
  const blocked = blockedReach(reach)
  if (reach?.state === 'fallback' && reach.credential) {
    return (
      <Banner severity="info" testId="project-git-reach">
        Tasks push as <strong>{reach.credential}</strong>: your selected GitHub connection can't write here.
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
        <ExternalLink href={reach.fix_url} color="inherit" underline="always">
          Fix
        </ExternalLink>
      )}
    </span>
  )
}
