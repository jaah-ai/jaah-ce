import Box from '@mui/material/Box'
import ExternalLink from '../../ui/ExternalLink'
import type { JSX, ReactNode } from 'react'
import { safeHref } from '../../lib/safeHref'
import { monoSx, ELLIPSIS_SX } from '../../ui/listStyles'
import { StatusChip, type Tone } from '../../ui/StatusChip'
import type { PipelineTask, TaskPr } from '../../types'
import { FONT_SIZE } from '../../theme'
import Tip from '../../ui/Tip'

/* A TASK'S PULL REQUESTS — one per repository it pushed to. The single home of how a PR reads in the
 * console: `owner/name #123` as a link, then its state. Two hosts: the detail page's Outcome and the epic's
 * Finisher notes (a lead per PR). */

/** `prs` is absent on an older payload; absence is no PRs. */
export function taskPrs(task: Pick<PipelineTask, 'prs'>): TaskPr[] {
  return task.prs ?? []
}

/* A row older than `prs` carries only `outcome_pr_url`, the primary's merged PR: read `owner/name #n` from the
 * URL path so it renders like every other PR. Each forge's PR marker as `app/forges.py` spells it (Azure's
 * `_git` segment dropped). Null when the URL names no PR, so the caller keeps its plain link. */
const PR_PATH = /^\/(.+?)\/(?:pull|-\/merge_requests|pull-requests|pullrequest)\/(\d+)(?:\/|$)/

export function legacyPr(url: string | null | undefined): TaskPr | null {
  if (!url) return null
  let path: string
  try {
    // Decoded here: a malformed `%` escape throws, and this runs while the page renders.
    path = decodeURIComponent(new URL(url).pathname)
  } catch {
    return null
  }
  const m = PR_PATH.exec(path)
  if (!m || Number(m[2]) < 1) return null
  return {
    repo_path: m[1].replace('/_git/', '/'),
    pr_number: Number(m[2]),
    pr_url: url,
    state: 'merged',
    is_primary: true,
  }
}

export function prLabel(pr: TaskPr): string {
  return `${pr.repo_path} #${pr.pr_number}`
}

/* Merged is the done reading (success); open is in flight (info); closed went nowhere (grey). */
const STATE_TONE: Record<TaskPr['state'], Tone> = { merged: 'success', open: 'info', closed: 'neutral' }

export function PrStateChip({ state }: { state: TaskPr['state'] }): JSX.Element {
  return (
    <Box component="span" data-testid="task-pr-state" data-state={state} sx={{ flex: '0 0 auto', display: 'inline-flex' }}>
      <StatusChip label={state} tone={STATE_TONE[state]} />
    </Box>
  )
}

/* The repository part ellipsises and the number never does: `#123` is what tells two PRs on one repo apart.
   GUARDED: `pr_url` is runner-reported, so an unusable URL is plain text (`lib/safeHref.ts`). */
function PrLink({ pr, testId }: { pr: TaskPr; testId: string }): JSX.Element {
  const href = safeHref(pr.pr_url)
  const label = prLabel(pr)
  const body = (
    <>
      <Box component="span" sx={ELLIPSIS_SX}>
        {pr.repo_path}
      </Box>
      <Box component="span" sx={{ flex: '0 0 auto', whiteSpace: 'pre' }}>{` #${pr.pr_number}`}</Box>
    </>
  )
  const sx = (theme: Parameters<typeof monoSx>[0]) => ({
    ...monoSx(theme),
    display: 'inline-flex',
    minWidth: 0,
    maxWidth: '100%',
    fontSize: FONT_SIZE.sm,
  })
  return href ? (
    <Tip title={label}><ExternalLink href={href} data-testid={testId} underline="hover" sx={sx}>
      {body}
    </ExternalLink></Tip>
  ) : (
    <Tip title={label}><Box component="span" data-testid={testId} sx={theme => ({ ...sx(theme), color: 'text.secondary' })}>
      {body}
    </Box></Tip>
  )
}

/** Every PR, one per line: link, state, and an optional lead (the Finisher's `child #id`). Null when empty. */
export function TaskPrList<P extends TaskPr>({
  prs,
  lead,
  testId = 'task-prs',
}: {
  prs: P[]
  lead?: (pr: P) => ReactNode
  testId?: string
}): JSX.Element | null {
  if (prs.length === 0) return null
  return (
    <Box component="ul" data-testid={testId} aria-label="Pull requests"
      sx={{ m: 0, p: 0, listStyle: 'none', display: 'flex', flexDirection: 'column', gap: 0.5, minWidth: 0 }}>
      {prs.map((pr, i) => (
        <Box component="li" key={`${i}:${pr.repo_path}#${pr.pr_number}`} data-testid="task-pr"
          sx={{ display: 'flex', alignItems: 'center', gap: 1, minWidth: 0 }}>
          {lead?.(pr)}
          <PrLink pr={pr} testId="task-pr-link" />
          <PrStateChip state={pr.state} />
        </Box>
      ))}
    </Box>
  )
}
