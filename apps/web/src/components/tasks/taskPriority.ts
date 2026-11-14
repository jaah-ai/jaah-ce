/* Task priority levels — web side of `apps/api/app/task_priority.py`; `test_task_priority.py` pins the two
 * tables together. Stored as a plain int (higher = sooner; dispatch orders `priority DESC, id`). */

import type { Explain } from '../../ui/explain'

/** The Priority label's explanation, on every surface that sets it (the task form, the row's Priority dialog). */
export const PRIORITY_HELP: Explain = {
  tip: 'Higher levels leave the queue first.',
  more: "Queued tasks dispatch by priority, then oldest first. An epic's priority passes to its subtasks.",
  docs: 'console-reference/tasks/new-task',
}

export type PriorityLevel = {
  /** The stored `Task.priority` int for this level. */
  value: number
  /** Operator-facing name in the selector. */
  label: string
}

/** What an absent/NULL `priority` means — the column's own server default. */
export const DEFAULT_PRIORITY = 0

/* High → low (selector order). Gaps let a raw int set outside the UI fall between levels. */
export const PRIORITY_LEVELS: readonly PriorityLevel[] = [
  { value: 20, label: 'Urgent' },
  { value: 10, label: 'High' },
  { value: 0, label: 'Normal' },
  { value: -10, label: 'Low' },
] as const

/** Level label, or `P<n>` for an unnamed int — same spelling as the API's `priority_label`. */
export function priorityLabel(priority: number | null | undefined): string {
  const value = priority ?? DEFAULT_PRIORITY
  return PRIORITY_LEVELS.find(level => level.value === value)?.label ?? `P${value}`
}
