import type { JSX } from 'react'
import { absoluteTime, compactAge } from '../../lib/relativeTime'
import RecordFeed from '../../ui/feed/RecordFeed'
import type { FeedColumns, FeedRowSpec } from '../../ui/feed/types'
import { PHONE } from '../../ui/listStyles'
import { grid } from '../../theme'
import {
  listTrackerConnections,
  NEW_TRACKER_PATH,
  mappedProjectCount,
  trackerLabel,
  trackerPath,
  TRACKERS_KEY,
  type TrackerConnection,
} from './api'

/* ISSUE TRACKERS — the org's tracker connections (Linear, Jira, Backlog), one feed row each (`GET
 * /api/tracker-connections`, `@org_admin`; the route is guarded the same in `App.tsx`). A row: the connection's name,
 * the tracker, its site, how many projects it maps to, and when it was connected; it opens the connection's tabbed
 * page (`TrackerDetailPage.tsx`). Add tracker opens the connect page (`TrackerNewPage.tsx`). `TrackerFeed` is
 * also Settings › Integrations › Issue trackers' body (`pages/Settings.tsx`). */

const COLUMNS: FeedColumns = {
  title: { key: 'name', label: 'Name' },
  slots: [
    { key: 'tracker', label: 'Tracker' },
    { key: 'site', label: 'Site' },
    { key: 'projects', label: 'Projects' },
    { key: 'created', label: 'Added', ariaLabel: 'Connected' },
  ],
}

function rowOf(conn: TrackerConnection): FeedRowSpec {
  const projects = mappedProjectCount(conn)
  return {
    key: conn.id,
    title: conn.name,
    titleText: conn.name,
    titleWrapAt: PHONE,
    href: trackerPath(conn.id),
    data: { tracker: conn.tracker },
    slots: [
      { key: 'tracker', width: grid(14), align: 'left', node: trackerLabel(conn.tracker), testId: 'tracker-kind' },
      { key: 'site', width: grid(30), gate: 'meta', align: 'left', dim: true, title: conn.site ?? undefined, node: conn.site ?? '—', testId: 'tracker-site' },
      { key: 'projects', width: grid(11), align: 'right', mono: true, title: `${projects} mapped`, node: String(projects), testId: 'tracker-projects' },
      { key: 'created', width: grid(8), gate: 'meta', align: 'right', mono: true, dim: true, title: absoluteTime(conn.created_at), node: compactAge(conn.created_at), testId: 'tracker-created' },
    ],
  }
}

const FEED = {
  id: 'trackers', label: 'Issue trackers', queryKey: TRACKERS_KEY, list: listTrackerConnections, rowOf, columns: COLUMNS,
  emptyTitle: 'No tracker connected.', newLabel: 'Add tracker', newTo: NEW_TRACKER_PATH,
}

/** The list's body by read state: loading, failed, none yet, or the rows. */
export const TrackerFeed = (): JSX.Element => <RecordFeed {...FEED} />

export default function TrackerList(): JSX.Element {
  return <RecordFeed {...FEED} page />
}
