import Box from '@mui/material/Box'
import ButtonBase from '@mui/material/ButtonBase'
import Link from '@mui/material/Link'
import Popover from '@mui/material/Popover'
import { useId, useState, type JSX, type ReactNode, type SyntheticEvent } from 'react'
import { safeHref } from '../../lib/safeHref'
import { monoSx } from '../../ui/listStyles'
import { StatusChip, type Tone } from '../../ui/StatusChip'
import type { PipelineTask, TaskPr } from '../../types'

/* A TASK'S PULL REQUESTS — one per repository it pushed to. The single home of how a PR reads in the
 * console: `owner/name #123` as a link, then its state. Three hosts: the detail page's Outcome (`TaskPrList`),
 * the Tasks list rows (`TaskPrSummary`: the first PR, then "+N" opening every other) and the epic's Finisher
 * notes (`TaskPrList` with a lead per PR). */

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
const DOT_COLOR: Record<TaskPr['state'], string> = { merged: 'success.main', open: 'info.main', closed: 'text.disabled' }

export function PrStateChip({ state }: { state: TaskPr['state'] }): JSX.Element {
  return (
    <Box component="span" data-testid="task-pr-state" data-state={state} sx={{ flex: '0 0 auto', display: 'inline-flex' }}>
      <StatusChip label={state} tone={STATE_TONE[state]} />
    </Box>
  )
}

/* The repository part ellipsises and the number never does: `#123` is what tells two PRs on one repo apart.
   GUARDED: `pr_url` is runner-reported, so an unusable URL is plain text (`lib/safeHref.ts`). */
/* `short` (the list row): the repository's own name — what tells a task's repositories apart, where the owner is
   usually shared — with the full `owner/name` in the tooltip and the "+N" popover. */
function PrLink({ pr, testId, short = false }: { pr: TaskPr; testId: string; short?: boolean }): JSX.Element {
  const href = safeHref(pr.pr_url)
  const label = prLabel(pr)
  const repo = short ? pr.repo_path.slice(pr.repo_path.lastIndexOf('/') + 1) : pr.repo_path
  const body = (
    <>
      <Box component="span" sx={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
        {repo}
      </Box>
      <Box component="span" sx={{ flex: '0 0 auto', whiteSpace: 'pre' }}>{` #${pr.pr_number}`}</Box>
    </>
  )
  const sx = (theme: Parameters<typeof monoSx>[0]) => ({
    ...monoSx(theme),
    display: 'inline-flex',
    minWidth: 0,
    maxWidth: '100%',
    fontSize: '0.75rem',
  })
  return href ? (
    <Link href={href} target="_blank" rel="noopener noreferrer" title={label} data-testid={testId} underline="hover" sx={sx}>
      {body}
    </Link>
  ) : (
    <Box component="span" title={label} data-testid={testId} sx={theme => ({ ...sx(theme), color: 'text.secondary' })}>
      {body}
    </Box>
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

/* Clicks inside never reach the row (its body opens the console, its title navigates). React bubbles through
   the popover's portal too, so the one handler on the wrapper covers the popover's links. */
const stop = (e: SyntheticEvent) => e.stopPropagation()

/** One row's PRs in one line: the first PR with a state dot, then "+N" listing EVERY other one. Null when none. */
export function TaskPrSummary({ prs, taskTitle }: { prs: TaskPr[]; taskTitle: string }): JSX.Element | null {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null)
  const popId = useId()
  if (prs.length === 0) return null
  const [first, ...others] = prs
  return (
    <Box
      component="span"
      data-testid="task-pr-summary"
      onClick={stop}
      onPointerDown={stop}
      onKeyDown={stop}
      sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.75, minWidth: 0, maxWidth: '100%', verticalAlign: 'middle' }}
    >
      <Box
        component="span"
        role="img"
        aria-label={first.state}
        title={first.state}
        data-testid="task-pr-dot"
        data-state={first.state}
        sx={{ flex: '0 0 auto', width: 7, height: 7, borderRadius: '50%', bgcolor: DOT_COLOR[first.state] }}
      />
      <PrLink pr={first} testId="task-pr-first" short />
      {others.length > 0 && (
        <>
          <ButtonBase
            type="button"
            aria-haspopup="dialog"
            aria-expanded={anchor !== null}
            aria-controls={anchor ? popId : undefined}
            aria-label={`${others.length} more pull request${others.length === 1 ? '' : 's'} of ${taskTitle}`}
            onClick={e => setAnchor(anchor ? null : e.currentTarget)}
            data-testid="task-pr-more"
            sx={theme => ({
              ...monoSx(theme),
              flex: '0 0 auto',
              fontSize: '0.6875rem',
              fontWeight: 600,
              color: 'primary.main',
              px: 0.5,
              borderRadius: 1,
              lineHeight: 1.5,
              '&:hover': { bgcolor: 'action.hover' },
              '&.Mui-focusVisible': { outline: '2px solid', outlineColor: 'primary.main', outlineOffset: 1 },
            })}
          >
            +{others.length}
          </ButtonBase>
          <Popover
            id={popId}
            open={anchor !== null}
            anchorEl={anchor}
            onClose={() => setAnchor(null)}
            anchorOrigin={{ vertical: 'bottom', horizontal: 'left' }}
            slotProps={{ paper: { sx: { p: 1.5, maxWidth: 'min(32rem, calc(100vw - 32px))', maxHeight: '60vh', overflowY: 'auto' } } }}
          >
            <TaskPrList prs={others} testId="task-pr-popover" />
          </Popover>
        </>
      )}
    </Box>
  )
}
