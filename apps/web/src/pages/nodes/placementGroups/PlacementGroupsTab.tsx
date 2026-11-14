import type { JSX } from 'react'
import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import { apiDetail } from '../../../api'
import { useCan } from '../../../auth'
import Banner from '../../../ui/Banner'
import EmptyState from '../../../ui/EmptyState'
import LoadingState from '../../../ui/LoadingState'
import NewButton from '../../../ui/NewButton'
import { FeedList } from '../../../ui/feed/FeedList'
import type { FeedColumns, FeedRowSpec, FeedSlot } from '../../../ui/feed/types'
import { GROUPS_KEY, listGroups, type GroupsList, type GroupRow } from './api'

/* /nodes › Groups (`?tab=node-groups`) — NODE GROUPS, the sets of nodes a project may run on. One `FeedList`: the
 * built-in All pinned first (computed, never stored, so no page: its row links nowhere), then each group, linking to
 * its page (`PlacementGroupDetailPage`). The trailing slots are `meta`: a phone or a narrow pane keeps the name and
 * the node count. The strip's New group is `Nodes.tsx`'s; an empty list carries its own. Not Pools (`../nodeGroups`). */

export const GROUPS_TAB_PATH = '/nodes?tab=node-groups'
export const NEW_GROUP_PATH = '/nodes/node-groups/new'
export const groupPath = (id: string) => `/nodes/node-groups/${encodeURIComponent(id)}`
/** Who may write a group: the routes' key. */
export const EDIT_KEY = 'nodes:edit' as const
export const NO_EDIT_REASON = 'Needs the nodes:edit permission'

const SHOWN_NAMES = 3
const NODES_SLOT = 240
const PROJECTS_SLOT = 200
const DESCRIPTION_SLOT = 220

// AC16's order: Name, Nodes (count + up to three names), Projects (count + names), Description.
const COLUMNS: FeedColumns = {
  title: { key: 'name', label: 'Name' },
  slots: [
    { key: 'nodes', label: 'Nodes' },
    { key: 'projects', label: 'Projects' },
    { key: 'description', label: 'Description' },
  ],
}

/** Up to three names, then `+N` for the rest of `total`. */
export function namesLine(names: readonly string[], total: number): string {
  const shown = names.slice(0, SHOWN_NAMES)
  const rest = total - shown.length
  return [shown.join(', '), rest > 0 ? `+${rest}` : ''].filter(Boolean).join(' ')
}

/** `3 · a, b, c` — the count first, so a narrow slot that ellipsizes the names still shows it. */
const counted = (count: number, names: string | null) => (names ? `${count} · ${names}` : String(count))

function slots(o: { description: string; members: string | null; projects: string | null; count: number; projectCount: number | null }): FeedSlot[] {
  const nodes = counted(o.count, o.members)
  const projects = o.projectCount === null ? null : counted(o.projectCount, o.projects)
  return [
    // Kept on a phone: the node count is the row's one fact beside its name.
    { key: 'nodes', width: NODES_SLOT, title: nodes, node: nodes, testId: 'node-group-nodes' },
    { key: 'projects', width: PROJECTS_SLOT, gate: 'meta', title: projects ?? undefined, node: projects, testId: 'node-group-projects' },
    { key: 'description', width: DESCRIPTION_SLOT, gate: 'meta', dim: true, title: o.description || undefined, node: o.description || null },
  ]
}

function allRow(all: GroupsList['all']): FeedRowSpec {
  return {
    key: 'all',
    title: 'All',
    titleText: 'All',
    ariaLabel: `All — Every node — ${all.node_count} nodes`,
    // Built in and computed: no record, so no page to open.
    href: null,
    data: { builtin: true },
    slots: slots({ description: 'Every node', members: null, projects: null, count: all.node_count, projectCount: null }),
  }
}

function groupRow(g: GroupRow): FeedRowSpec {
  const members = g.node_count > 0 ? namesLine(g.node_names, g.node_count) : null
  const projects = g.project_count > 0 ? g.project_names.join(', ') : null
  return {
    key: g.id,
    title: g.name,
    titleText: g.name,
    titleLines: 2,
    ariaLabel: `${g.name}${g.description ? ` — ${g.description}` : ''} — ${g.node_count} nodes — ${g.project_count} projects`,
    href: groupPath(g.id),
    slots: slots({ description: g.description, members, projects, count: g.node_count, projectCount: g.project_count }),
  }
}

export default function PlacementGroupsTab(): JSX.Element {
  const canEdit = useCan()(EDIT_KEY)
  const query = useQuery({ queryKey: GROUPS_KEY, queryFn: listGroups })
  const data = query.data

  return (
    <Box component="section" data-testid="placement-groups-panel" sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: 2 }}>
      {query.isError && <Banner severity="error">{apiDetail(query.error, 'Failed to load the node groups.')}</Banner>}
      {query.isPending && <LoadingState rows={3} />}
      {data && (
        <FeedList
          ariaLabel="Node groups"
          testId="placement-groups-list"
          columns={COLUMNS}
          sections={[{ key: 'groups', rail: false, rows: [allRow(data.all), ...data.groups.map(groupRow)] }]}
        />
      )}
      {data && data.groups.length === 0 && (
        <EmptyState
          title="No node groups yet. Group nodes to choose where each project runs."
          action={(
            <NewButton label="New group" worded to={NEW_GROUP_PATH} testId="placement-groups-new"
              disabled={!canEdit} title={canEdit ? undefined : NO_EDIT_REASON} />
          )}
        />
      )}
    </Box>
  )
}
