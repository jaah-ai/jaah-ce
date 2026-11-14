// The task form's issue picker client (consumer: `components/trackers/IssuePicker.tsx`). Project-scoped and
// tracker-neutral: `apps/api/app/routes/project_trackers.py` asks every connection the project links.

import { api } from '../../api'

/** One search hit. `tracker` keys the mark; `tracker_label` is the server's word for it. */
export type TrackerIssueHit = {
  connection_id: string
  tracker: string
  tracker_label: string
  key: string
  title: string
  url: string
}

/** A linked connection that could not answer the search: its own sentence, the rest still list. */
export type TrackerIssueSearchError = {
  connection_id: string
  tracker_label: string
  name: string
  detail: string
}

export type TrackerIssueSearch = { results: TrackerIssueHit[]; errors: TrackerIssueSearchError[] }

/** One picked issue with its description (Markdown, what `tasks.body` stores; `''` when it has none). */
export type TrackerIssueText = TrackerIssueHit & { body_markdown: string }

/** What a task create/update sends as `tracker_issue` (`routes/tasks/tracker_pick.py::TrackerIssuePick`). */
export type TrackerIssuePick = { connection_id: string; key: string }

/** A connection as one phrase: its name, led by the tracker's label unless the name already starts with it
 *  (a connection is named "Jira · site" by default; "Jira · Jira · site" would say it twice). */
export function connectionLabel(c: { tracker_label: string; name: string }): string {
  return c.name === c.tracker_label || c.name.startsWith(`${c.tracker_label} · `) ? c.name : `${c.tracker_label} · ${c.name}`
}

/** Server default page size. */
export const TRACKER_SEARCH_LIMIT = 15

/* `api`, not `get`, to pass the abort signal `ui/AsyncLookup` hands its fetcher. */
export const searchTrackerIssues = (projectId: number, q: string, signal: AbortSignal) =>
  api<TrackerIssueSearch>(
    `/api/projects/${projectId}/tracker-issues?q=${encodeURIComponent(q)}&limit=${TRACKER_SEARCH_LIMIT}`,
    { signal },
  )

/** 404 outside the project's linked tracker projects; 502 with the tracker's own sentence. */
export const getTrackerIssue = (projectId: number, connectionId: string, key: string) =>
  api<TrackerIssueText>(
    `/api/projects/${projectId}/tracker-issues/${encodeURIComponent(connectionId)}/${encodeURIComponent(key)}`,
  )
