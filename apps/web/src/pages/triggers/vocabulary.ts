import { readTaskSources } from '../../components/tasks/shared'
import { useApiRead } from '../../hooks/useApiRead'
import { capitalize } from '../../lib/humanize'

/* THE TRIGGERS SURFACE'S WORDS. Every label a user reads is the SERVER's where the server has one:
 * a source kind's label is the task Source filter's (`GET /api/tasks/sources`, `trigger:<kind>` →
 * `app/task_source.py::SOURCE_LABELS`), a delivery outcome's is on the row and the log page
 * (`app/triggers.py::OUTCOME_LABELS`), and health carries its own reason sentence. What is spelled here is
 * only what the server has no word for: the signing kinds, the health tone, and the health state's
 * capitalisation. */

/** Kinds that sign their requests and so carry a URL and a secret (`app/triggers.py::HTTP_SOURCE_KINDS`). */
export const HTTP_SOURCE_KINDS: ReadonlySet<string> = new Set(['webhook', 'github'])

/** `trigger:<kind>` → label, from the cached vocabulary (`readTaskSources`); unread, the kind capitalised. */
export function useSourceKindLabels(): (kind: string) => string {
  const { data: rows } = useApiRead(['trigger-source-labels'], readTaskSources, 'failed')
  return (kind: string) => rows?.find(row => row.value === `trigger:${kind}`)?.label ?? capitalize(kind)
}

/* GitHub's own names for the webhook events a trigger listens to, as its "Let me select individual events"
 * checklist spells them — the setup panel tells the owner which boxes to tick. An event GitHub names
 * otherwise (or one added later) shows as its wire name, which is what the `X-GitHub-Event` header carries. */
const GITHUB_EVENT_LABELS: Record<string, string> = {
  issues: 'Issues',
  issue_comment: 'Issue comments',
  pull_request: 'Pull requests',
  pull_request_review: 'Pull request reviews',
  pull_request_review_comment: 'Pull request review comments',
  push: 'Pushes',
  release: 'Releases',
  discussion: 'Discussions',
  discussion_comment: 'Discussion comments',
  workflow_run: 'Workflow runs',
  check_run: 'Check runs',
  check_suite: 'Check suites',
  create: 'Branch or tag creation',
  delete: 'Branch or tag deletion',
  label: 'Labels',
  milestone: 'Milestones',
}

export function githubEventLabel(event: string): string {
  return GITHUB_EVENT_LABELS[event] ?? event
}
