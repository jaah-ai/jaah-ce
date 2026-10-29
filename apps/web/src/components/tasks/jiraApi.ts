// The task form's "From Jira" lookup client (consumer: `components/tasks/TaskEditForm.tsx`).
// Project-scoped: the create surface has no task id yet (`apps/api/app/routes/jira.py`).

import { api } from '../../api'

/* Typeahead row, `GET /api/projects/{id}/jira/issues`. `status` stays nullable: the server fetches it
 * best-effort and answers `null` rather than failing the lookup. */
export type JiraIssueRef = {
  key: string
  summary: string
  status: string | null
}

/* `GET /api/projects/{id}/jira/issues/{key}`. `description` is Markdown (what `tasks.body` stores), and
 * `''` when the issue has none; the form then leaves the operator's text alone. */
export type JiraIssueText = {
  key: string
  summary: string
  description: string
  url: string
}

/* Server default page size (bounded 1..50 server-side). */
export const JIRA_SEARCH_LIMIT = 15

/* `api`, not `get`, to pass the abort signal `ui/AsyncLookup` hands its fetcher. A blank `q` is handled
   server-side (empty list, no outbound call). */
export const searchJiraIssues = (projectId: number, q: string, signal: AbortSignal) =>
  api<JiraIssueRef[]>(
    `/api/projects/${projectId}/jira/issues?q=${encodeURIComponent(q)}&limit=${JIRA_SEARCH_LIMIT}`,
    { signal },
  )

/* Key is path-encoded. Failures the form has copy for: 409 (no Jira credential), 404 (Jira answers the
   same for missing and forbidden, so copy must not claim absence), 502/503. */
export const getJiraIssue = (projectId: number, issueKey: string) =>
  api<JiraIssueText>(`/api/projects/${projectId}/jira/issues/${encodeURIComponent(issueKey)}`)
