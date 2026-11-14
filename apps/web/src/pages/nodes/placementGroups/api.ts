// NODE GROUPS' ONE API CLIENT (code name `placement_groups`; the routes are the API's placement-groups route module,
// cloud only). Not Pools: those are `../nodeGroups/api.ts` (`/api/node-groups`). Every shape here mirrors the
// route module's pydantic models; a 409 that asks to confirm a move carries `MovePreview` (`moveNodes`).

import type { QueryClient } from '@tanstack/react-query'
import { ApiError, del, get, patch, post, put } from '../../../api'

/** The built-in group every node is in: computed, never stored, never edited. */
export type GroupAll = { name: 'All'; node_count: number }
export type GroupRef = { id: string; name: string; slug: string }
export type GroupRow = GroupRef & {
  description: string
  node_count: number
  /** The first three member names, by name. */
  node_names: string[]
  project_count: number
  project_names: string[]
}
/** `status`: `online` | `offline` | `unknown`, or `disabled`. */
export type GroupNode = { id: string; name: string; status: string }
export type GroupDetail = GroupRow & { nodes: GroupNode[]; projects: { id: number; name: string }[] }
export type GroupsList = { all: GroupAll; groups: GroupRow[] }
/** A project's groups: `mode` `all` with no groups, else its groups. `active` false = kept while the plan is off.
 * `nodes`: the ones its tasks can start on now, as placement computes them, narrowed to those the viewer may see;
 * `node_count` counts every one. */
export type ProjectGroups = {
  mode: 'all' | 'groups'
  groups: GroupRef[]
  active: boolean
  editable: boolean
  nodes: GroupNode[]
  node_count: number
}
/** One node a change takes away while tasks use it. A node the viewer cannot see has no id and no name. */
export type MoveNode = {
  id: string | null
  name: string
  tasks_in_use: number
  projects: { id: number; name: string }[]
  hidden_projects: number
}

export const GROUPS_KEY = ['placement-groups'] as const
export const groupKey = (id: string) => ['placement-groups', id] as const
export const projectGroupsKey = (projectId: number) => ['project-placement-groups', projectId] as const
export const nodeGroupsKey = (nodeId: string) => ['node-placement-groups', nodeId] as const

const enc = encodeURIComponent

/** After any group write: the list and each group, every project's and node's groups, and the nodes a task may be
 *  pinned to (`TaskSpecFields`), which follow a project's groups. `fresh`: a key the caller just set from the write's
 *  own answer, left alone so it is not read again. */
export function refreshAfterGroupWrite(client: QueryClient, fresh?: readonly unknown[]): Promise<unknown> {
  const same = (key: readonly unknown[]) => fresh !== undefined && JSON.stringify(key) === JSON.stringify(fresh)
  return Promise.all([GROUPS_KEY, ['project-placement-groups'], ['node-placement-groups'], ['task-runnable-nodes']]
    .map(queryKey => client.invalidateQueries({ queryKey, predicate: q => !same(q.queryKey) })))
}

export async function listGroups(): Promise<GroupsList> {
  const out = await get<GroupsList>('/api/placement-groups')
  // Shape-checked once here: the Groups tab and the roster's sections share this cache.
  if (!out || !Array.isArray(out.groups) || typeof out.all?.node_count !== 'number') {
    throw new Error('The node groups came back in an unexpected shape.')
  }
  return out
}

export const getGroup = (id: string) => get<GroupDetail>(`/api/placement-groups/${enc(id)}`)

export const createGroup = (body: { name: string; description?: string; node_ids?: string[] }) =>
  post<GroupDetail>('/api/placement-groups', body)

export const updateGroup = (
  id: string,
  body: { name?: string; description?: string; node_ids?: string[]; confirm?: boolean },
) => patch<GroupDetail>(`/api/placement-groups/${enc(id)}`, body)

/** Without `confirm`, a group projects use answers 409 `{projects}` (`usedBy`). */
export const deleteGroup = (id: string, confirm = false) =>
  del(`/api/placement-groups/${enc(id)}${confirm ? '?confirm=true' : ''}`)

export const getProjectGroups = (projectId: number) =>
  get<ProjectGroups>(`/api/projects/${projectId}/placement-groups`)

/** `group_ids: []` is All. */
export const putProjectGroups = (projectId: number, group_ids: string[], confirm = false) =>
  put<ProjectGroups>(`/api/projects/${projectId}/placement-groups`, { group_ids, confirm })

export const getNodeGroups = (nodeId: string) =>
  get<{ groups: GroupRef[] }>(`/api/nodes/${enc(nodeId)}/placement-groups`)

export const putNodeGroups = (nodeId: string, group_ids: string[], confirm = false) =>
  put<{ groups: GroupRef[] }>(`/api/nodes/${enc(nodeId)}/placement-groups`, { group_ids, confirm })

const detailOf = (err: unknown): Record<string, unknown> | null =>
  err instanceof ApiError && err.status === 409 && err.detail !== null && typeof err.detail === 'object'
    ? err.detail as Record<string, unknown>
    : null

/** The nodes a refused change would take away while tasks run there (a 409 `MovePreview`), else null. */
export function moveNodes(err: unknown): MoveNode[] | null {
  const nodes = detailOf(err)?.nodes
  return Array.isArray(nodes) ? nodes as MoveNode[] : null
}

/** The 422 on a name (`{field: 'name', detail}`), for an inline error under the field. */
export function nameError(err: unknown): string | null {
  if (!(err instanceof ApiError) || err.status !== 422) return null
  const d = err.detail as { field?: unknown; detail?: unknown } | null
  return d && d.field === 'name' && typeof d.detail === 'string' ? d.detail : null
}
