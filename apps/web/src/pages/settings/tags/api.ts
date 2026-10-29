// Tags API client. Record shapes live in `src/types.ts`; the request shapes here encode server
// boundary decisions (no `state` on create; absent `tag` derives the code; reorder takes the whole list).

import { del, get, post, put } from '../../../api'
import type { Tag, TagGroup } from '../../../types'

/** `GET /api/tags/groups/{id}/usage` and `/api/tags/{id}/usage` — QUERIED, never estimated. */
export type Usage = { task_count: number }

/** No `state`: a group is BORN draft server-side, and `extra="forbid"` refuses the field outright. */
export type TagGroupCreateBody = {
  name: string
  description: string
  multi_select: boolean
}

/** A partial update — only the keys present are touched. `state` is how a draft group goes live. */
export type TagGroupUpdateBody = {
  name?: string
  description?: string
  state?: TagGroup['state']
  multi_select?: boolean
}

/** Omit `tag` to derive the code from the name; `''` is a 422, so a blank field must omit the key. */
export type TagCreateBody = {
  name: string
  tag?: string
  description: string
}

export type TagUpdateBody = {
  tag?: string
  name?: string
  description?: string
}

/** `?include_draft=true` is allowed for `tags:view` OR superadmin on top of `tasks:view`
 *  (`apps/api/app/routes/tags.py`; its docstring says why). */
export const listTagGroups = () => get<TagGroup[]>('/api/tags/groups?include_draft=true')

export const createTagGroup = (body: TagGroupCreateBody) => post<TagGroup>('/api/tags/groups', body)

export const updateTagGroup = (id: string, body: TagGroupUpdateBody) =>
  put<TagGroup>(`/api/tags/groups/${id}`, body)

export const deleteTagGroup = (id: string) => del(`/api/tags/groups/${id}`)

export const tagGroupUsage = (id: string) => get<Usage>(`/api/tags/groups/${id}/usage`)

export const createTag = (groupId: string, body: TagCreateBody) =>
  post<Tag>(`/api/tags/groups/${groupId}/tags`, body)

export const updateTag = (id: string, body: TagUpdateBody) => put<Tag>(`/api/tags/${id}`, body)

export const deleteTag = (id: string) => del(`/api/tags/${id}`)

export const tagUsage = (id: string) => get<Usage>(`/api/tags/${id}/usage`)

/* Reorders send the complete list in one request: two single-row `position` writes are not atomic, and
 * a tie falls back to alphabetical. The server renumbers 1..n in one transaction and refuses a partial list. */
export const reorderTags = (groupId: string, tagIds: string[]) =>
  put<TagGroup>(`/api/tags/groups/${groupId}/tag-order`, { tag_ids: tagIds })

export const reorderTagGroups = (groupIds: string[]) =>
  put<TagGroup[]>('/api/tags/group-order', { group_ids: groupIds })

/** The form fields a server refusal can be attributed to. */
export type TagFormField = 'name' | 'tag' | 'description' | 'multi_select' | 'state'

/* Which field a refusal belongs to, matched on the server's `detail` sentence (`ui/FormField` renders
 * messages only in a field). Presentation only: unmatched → `null`, shown at the top of the form.
 * Order is load-bearing: "already exists in tag group" must not match as a group-name collision, and
 * "tag group 'X' has no tags…" must hit the activation arm before `^tag `. */
export function fieldForDetail(detail: string): TagFormField | null {
  if (/already exists in tag group/.test(detail)) return 'tag'
  if (/^a tag group named/.test(detail)) return 'name'
  if (/cannot become single-select/.test(detail)) return 'multi_select'
  if (/cannot be activated/.test(detail) || /^state /.test(detail)) return 'state'
  if (/^name /.test(detail)) return 'name'
  if (/^description /.test(detail)) return 'description'
  if (/^tag(?! group)/.test(detail) || /no tag code can be derived/.test(detail)) return 'tag'
  return null
}
