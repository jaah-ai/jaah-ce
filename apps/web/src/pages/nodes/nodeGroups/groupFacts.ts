// What a node group's row and its page both say about it — one home, so `/nodes` › Groups and
// `/nodes/groups/:groupId` cannot disagree on a group's state, its shapes or why it cannot be deleted.

import type { Tone } from '../../../ui/StatusChip'
import type { NodeGroupRow } from '../../../types'

/** On a greyed control's `title` and the ⋯ item's name — a greyed button must say why. */
export const NO_EDIT_REASON = 'Administrator-only'

export type Convergence = { tone: Tone; glyph: string; label: string }

/** The group's standing against its instruction, as the disc's tone, glyph and words.
 *  DISABLED WINS OVER AN ERROR: a parked group is not failing. `last_error` still shows, muted, marked as
 *  history. */
export function convergence(row: NodeGroupRow): Convergence {
  if (!row.enabled) return { tone: 'neutral', glyph: '○', label: 'Switched off' }
  if (row.last_error !== '') return { tone: 'error', glyph: '✕', label: 'Not converging' }
  if (row.node_count < row.desired_total) return { tone: 'info', glyph: '»', label: 'Starting instances' }
  if (row.node_count > row.desired_total) return { tone: 'warning', glyph: '»', label: 'Handing nodes back' }
  return { tone: 'success', glyph: '●', label: 'At capacity' }
}

/** One capacity line in words: `On-demand c6g.2xlarge`. */
export const capacityLineLabel = (line: NodeGroupRow['capacity'][number]): string =>
  `${line.market === 'spot' ? 'Spot' : 'On-demand'} ${line.instance_type}`

/** The capacity table in one slot; the whole table rides the slot's `title` and the group's page. */
export function shapesLabel(row: NodeGroupRow): string {
  if (row.capacity.length === 0) return 'no shapes'
  if (row.capacity.length === 1) return capacityLineLabel(row.capacity[0])
  return `${row.capacity.length} shapes`
}

export function shapesTitle(row: NodeGroupRow): string {
  if (row.capacity.length === 0) return 'No capacity'
  return row.capacity
    .map(l => `${l.desired}x ${l.market === 'spot' ? 'spot' : 'on-demand'} ${l.instance_type} (${l.node_count} running)`)
    .join(' · ')
}

/** Projects have no slot; this is their one surface on the row (accessible name and hover). */
export function projectsTitle(row: NodeGroupRow): string {
  if (row.projects.length === 0) return 'no projects assigned'
  return row.projects.map(p => p.project_name).join(' · ')
}

/** What a group launches through, by name: its connection, else (a row from before connections) the
 *  AWS account. */
export const groupAccount = (row: NodeGroupRow): string =>
  row.connection_name || row.credential_name || `credential ${row.credential_id}`

/** Why a Delete cannot succeed, or `''`. The route 409s while any line has nodes, and `node_count` is the
 *  same number — so the answer (what blocks, the order that clears it) is given instead of a confirm. */
export function deleteBlockedReason(row: NodeGroupRow): string {
  if (row.node_count === 0) return ''
  return `${row.name} still has ${row.node_count} running instance${row.node_count === 1 ? '' : 's'}. `
    + 'Set every capacity line to 0 instances, wait, then delete — or switch the group off in Edit.'
}

/** The confirm's sentence: nothing is terminated (the guard proved it), so what is LOST and the reversible
 *  alternative. */
export function deleteQuestion(row: NodeGroupRow): string {
  return `Its capacity (${row.desired_total} instance${row.desired_total === 1 ? '' : 's'}) and `
    + `${row.projects.length} project assignment${row.projects.length === 1 ? '' : 's'} are lost for good. `
    + 'Nothing is running, so nothing is terminated. To pause instead, switch it off in Edit.'
}

/** A group's page, the row's link. */
export const groupPath = (id: string): string => `/nodes/groups/${encodeURIComponent(id)}`
