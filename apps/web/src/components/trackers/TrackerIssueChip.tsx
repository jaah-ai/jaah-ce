import type { JSX } from 'react'
import type { TaskTrackerIssue } from '../../types'
import ExternalLink from '../../ui/ExternalLink'
import StatusChip from '../../ui/StatusChip'

/* A TASK'S TRACKER ISSUE, AS ONE CHIP: the tracker's word and the issue key, opening the issue in a new tab. The same
 * for a tracker-started task and a hand-linked one (`PipelineTask.tracker_issue`): the task page's header and the
 * task form's read-only link. The href passes `safeHref` (inside `ExternalLink`); refused, the chip draws unlinked. */
export default function TrackerIssueChip({ issue }: {
  issue: Pick<TaskTrackerIssue, 'tracker_label' | 'key' | 'url'>
}): JSX.Element {
  const name = `${issue.tracker_label} ${issue.key}`
  return (
    <ExternalLink href={issue.url} underline="none" data-testid="task-tracker-issue" aria-label={`Open ${name} (new tab)`}
      sx={{ display: 'inline-flex', flexShrink: 0 }}>
      <StatusChip label={name} glyph="↗" tone="neutral" title={`Open ${name}`} />
    </ExternalLink>
  )
}
