import type { TaskTree, TaskTreeCounts, TaskTreeProject } from '../../../types'

/* A small task tree for the pane-header tests: `acme` has unfiled tasks (so a "No feature" node), `df` has none.
 * Permissions are per test via `withAccess`. */

export const FEAT_RAIL = '44444444-4444-4444-8444-444444444444'
export const FEAT_LONG = '55555555-5555-4555-8555-555555555555'
export const FEAT_DF = '66666666-6666-4666-8666-666666666666'
export const LONG_FEATURE_NAME = 'Pane header breadcrumb with type-ahead sibling menus and filter chips'

const ZERO: TaskTreeCounts = { stopped: 0, questions: 0, failed: 0, paused: 0, waiting: 0, in_progress: 0, draft: 0, done: 0, total: 0 }

function project(id: number, name: string, features: TaskTreeProject['features'], unfiled: TaskTreeCounts | null): TaskTreeProject {
  return { id, name, can_edit_project: true, can_create_tasks: true, can_edit_tasks: true, features, unfiled, counts: ZERO }
}

function feature(id: string, name: string, status: 'active' | 'shipped' = 'active') {
  return { id, name, status, show_progress: false, lead_id: 'u', shipped_at: null, counts: ZERO }
}

export const TREE: TaskTree = {
  can_create_project: true,
  counts: ZERO,
  projects: [
    project(7, 'acme', [feature(FEAT_RAIL, 'Rail tree'), feature(FEAT_LONG, LONG_FEATURE_NAME, 'shipped')], { ...ZERO, total: 2 }),
    project(9, 'df', [feature(FEAT_DF, 'Billing')], null),
  ],
}

/** The tree as a user with `tasks:create` granted (or not) everywhere. */
export function withAccess({ createTasks }: { createTasks: boolean }): TaskTree {
  return {
    ...TREE,
    projects: TREE.projects.map(p => ({ ...p, can_create_tasks: createTasks, can_edit_tasks: createTasks })),
  }
}
