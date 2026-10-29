// Assignments — which projects a node runs and which nodes a project runs on. One relationship, two
// doors: `GET|PUT /api/nodes/{node_id}/projects` (the uuid) and `GET|PUT /api/projects/{id}/nodes` (`routes/assignments.py`).
// React-free: the wire shapes, the wire↔list mapping and the save outcome's words. The hook is
// `hooks/useAssignments.ts`; the control is `components/AssignmentList.tsx`.

import { countOf } from './humanize'

/** Which page is asking: a node lists projects, a project lists nodes. */
export type AssignmentSide = 'node' | 'project'

/** One assignment as the API sends it (`AssignmentOut`). */
export type AssignmentRecord = {
  node_id: string
  node_name: string
  /** The node is a MicroVM node (`Node.microvm`). */
  node_microvm?: boolean
  project_id: number
  project_name: string
  /** The static-slots override, the one static control; null = none. */
  static_slots: number | null
  /** The override, or 0. */
  effective_static: number
  /** Slots of this pair in use (a task or a person holds them): what removing it would destroy. */
  held: number
  /** Every run removing the pair kills — held slots and live MicroVMs (`LiveRunOut`). */
  live: LiveRun[]
  /** The node's group carries this project; only the group line unassigns it. */
  via_group: { id: string; name: string } | null
  removable: boolean
}

/** One run removing a pair kills: a task, a task the caller may not see (`hidden`), or a console session (neither);
 *  `vm` when it runs in a MicroVM. */
export type LiveRun = { task_id: number | null; title: string | null; hidden: boolean; vm: boolean }

/** What a save's converge did (`OutcomeOut`). */
export type AssignmentOutcome = {
  created: number
  destroyed: number
  demoted: number
  tasks_requeued: number[]
  warnings: string[]
}

/** `AssignmentsOut`; a PUT adds `outcome`. */
export type AssignmentsResponse = {
  assignments: AssignmentRecord[]
  /** `microvm` on a node candidate only. */
  available: { id: number | string; name: string; microvm?: boolean | null }[]
  /** Still onboarding (D15): new nodes and projects are assigned automatically. */
  onboarding_active: boolean
  outcome?: AssignmentOutcome
}

/** An unassigned candidate. Ids are strings on both sides (a project id is a number on the wire). */
export type TransferCandidate = { id: string; name: string; microvm?: boolean }

/** An assigned row as the control draws it. */
export type AssignmentItem = {
  id: string
  name: string
  /** A node row that is a MicroVM node; unset on a project row. */
  microvm?: boolean
  /** The static-slots override; null = none (the pair holds no static slot). */
  staticSlots: number | null
  /** Slots in use; 0 on a row assigned in this session until the server answers. */
  held: number
  /** The runs removing it kills; empty on a row assigned in this session. */
  live: LiveRun[]
  viaGroup: { id: string; name: string } | null
  removable: boolean
}

/** Both halves of the list: what is assigned and what could be. */
export type AssignmentDraft = { available: TransferCandidate[]; selected: AssignmentItem[] }

/** The read URL and the replace-set URL are the same path. */
export function assignmentsPath(side: AssignmentSide, subject: string | number): string {
  return side === 'node'
    ? `/api/nodes/${encodeURIComponent(String(subject))}/projects`
    : `/api/projects/${encodeURIComponent(String(subject))}/nodes`
}

/** A roster's sort: name, case-insensitive, `2` before `10`. */
export const byName = (a: { name: string }, b: { name: string }) =>
  a.name.localeCompare(b.name, undefined, { sensitivity: 'base', numeric: true })

/** The draft a response seeds: each row named by the OTHER side. */
export function draftFromResponse(side: AssignmentSide, response: AssignmentsResponse): AssignmentDraft {
  return {
    available: response.available
      .map(c => ({ id: String(c.id), name: c.name, ...(c.microvm ? { microvm: true } : {}) })).sort(byName),
    selected: response.assignments.map(a => ({
      id: side === 'node' ? String(a.project_id) : a.node_id,
      name: side === 'node' ? a.project_name : a.node_name,
      ...(side === 'project' && a.node_microvm ? { microvm: true } : {}),
      staticSlots: a.static_slots,
      held: a.held,
      live: a.live,
      viaGroup: a.via_group,
      removable: a.removable,
    })).sort(byName),
  }
}

/** The PUT body: every assigned row, the override riding along (null = none). */
export function assignmentsBody(side: AssignmentSide, draft: AssignmentDraft): { assignments: object[] } {
  return {
    assignments: draft.selected.map(item => side === 'node'
      ? { project_id: Number(item.id), static_slots: item.staticSlots }
      : { node_id: item.id, static_slots: item.staticSlots }),
  }
}

/** "2 created, 1 destroyed, 1 task requeued" — only what happened; empty when nothing did. */
export function summariseOutcome(outcome: AssignmentOutcome | undefined): string {
  if (!outcome) return ''
  const parts: string[] = []
  if (outcome.created) parts.push(`${outcome.created} created`)
  if (outcome.destroyed) parts.push(`${outcome.destroyed} destroyed`)
  if (outcome.demoted) parts.push(`${outcome.demoted} demoted`)
  if (outcome.tasks_requeued.length) {
    parts.push(`${countOf(outcome.tasks_requeued.length, 'task')} requeued`)
  }
  return parts.length ? `${parts.join(', ')}.` : ''
}

/** The whole line a save reports: what happened, then the server's warnings. */
export function outcomeNote(outcome: AssignmentOutcome | undefined): string {
  return [summariseOutcome(outcome), ...(outcome?.warnings ?? [])].filter(Boolean).join(' ')
}

/** The non-blocking warning for an empty assigned list once onboarding is over (D4); null otherwise. */
export function emptyAssignmentWarning(
  side: AssignmentSide, draft: AssignmentDraft | null, onboardingActive: boolean,
): string | null {
  if (!draft || onboardingActive || draft.selected.length > 0) return null
  return side === 'project' ? "No nodes — tasks for this project won't run." : "No projects — this node won't run tasks."
}

/** The line beside the control naming what a removal costs (D9). Stated, and confirmed only where slots are in
 *  use (`components/AssignmentList.tsx`). */
export function destructiveHelp(side: AssignmentSide): string {
  return `Removing a ${side === 'node' ? 'project' : 'node'} deletes its slots, even running ones. Lowering slots removes only idle ones.`
}

/** How many runs the confirmation names before "and N more". */
const NAMED_RUNS = 3

/** The removal confirmation: a line, then the runs it kills by name (the callout's bullets); with no runs known,
 *  the count of held slots and no names. */
export function removalWarning(item: Pick<AssignmentItem, 'held' | 'live'>): { line: string; names: string[] } {
  if (item.live.length === 0) return { line: `${item.held} in use. Removing deletes ${item.held === 1 ? 'it' : 'them'}.`, names: [] }
  const tasks = item.live.filter(run => run.task_id !== null)
  const named = tasks.slice(0, NAMED_RUNS).map(run => {
    const name = `#${run.task_id}${run.title ? ` ${run.title}` : ''}`
    return run.vm ? `${name} (VM)` : name
  })
  const counted = (n: number, one: string, many: string) => (n === 0 ? [] : [n === 1 ? one : `${n} ${many}`])
  const others = [
    ...counted(tasks.length - named.length, '1 more task', 'more tasks'),
    ...counted(item.live.filter(run => run.hidden).length, '1 task you can’t see', 'tasks you can’t see'),
    ...counted(item.live.filter(run => run.task_id === null && !run.hidden).length, 'a console session', 'console sessions'),
  ]
  return { line: 'Removing kills:', names: [...named, ...others] }
}
