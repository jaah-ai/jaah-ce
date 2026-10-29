import type { PipelineTask } from '../../types'

// Where a task's memory arm came from (`app/project_memory/vocabulary.py::ARM_SOURCES`), as the task view says it.
const ARM_SOURCE_LABELS: Record<string, string> = {
  task: 'set on task',
  epic: 'from epic',
  default: 'default',
  holdout: 'holdout',
  killed: 'switched off',
}

/* The task view's per-task memory record, e.g. "On (default) · 5 injected · 2 read · 1 cited". Null until a
 * dispatch stamped the arm, so a task that never ran under the switch shows nothing. */
export function memoryRecordLine(task: Pick<PipelineTask, 'memory_arm' | 'memory_arm_source' | 'memory_counts'>): string | null {
  if (task.memory_arm == null) return null
  const source = task.memory_arm_source ? ARM_SOURCE_LABELS[task.memory_arm_source] ?? task.memory_arm_source : null
  const arm = `${task.memory_arm ? 'On' : 'Off'}${source ? ` (${source})` : ''}`
  const c = task.memory_counts
  return c ? [arm, `${c.injected} injected`, `${c.read} read`, `${c.cited} cited`].join(' · ') : arm
}
