/* WHO FILED A TASK, in three classes the `/tasks` board tells apart on every row:
 *   · human     — filed in the console. Full strength.
 *   · automated — a schedule, a trigger (webhook, GitHub, email, Slack, new user) or an agent.
 *   · system    — the platform reporting on itself (incident, deploy failed, health breach) or a system
 *                 persona's bookkeeping.
 * People's work stands out: a machine-filed row's title is grey (`FeedRowSpec.quiet`), and a glyph in the
 * same grey takes the creator's place in the Creator → Assignee cell (`components/tasks/TaskPeople.tsx`).
 * Reads `source` (`apps/api/app/task_source.py`, the single writer) and the persona; React-free. */
import type { PipelineTask } from '../types'
import { isSystemPersona } from '../components/tasks/personaRegistry'

export type OriginKind = 'human' | 'automated' | 'system'

export type TaskOrigin = {
  kind: OriginKind
  /** The glyph drawn in the creator's place; `''` for a human row (its creator's initials show). */
  glyph: string
  /** The hover and accessible words: the server's source label plus the filer's name where kept
   *  ("Schedule: Nightly audit"). `''` for a human row. */
  label: string
}

/* The trigger kinds that are the platform reporting on itself, not a person's automation. */
const SYSTEM_SOURCES = new Set(['trigger:incident', 'trigger:deploy_failed', 'trigger:health_breach'])

const HUMAN: TaskOrigin = { kind: 'human', glyph: '', label: '' }

function named(kind: string, name: string | undefined): string {
  return name ? `${kind}: ${name}` : kind
}

export function taskOrigin(
  task: Pick<
    PipelineTask,
    'source' | 'source_label' | 'persona' | 'created_by_trigger_name' | 'created_by_automation_name' | 'created_by_cron_name'
  >,
): TaskOrigin {
  const source = task.source ?? 'console'
  const label = task.source_label ?? source
  if (SYSTEM_SOURCES.has(source)) return { kind: 'system', glyph: '⚙', label: named(label, task.created_by_trigger_name) }
  if (isSystemPersona(task.persona)) return { kind: 'system', glyph: '⚙', label: 'System' }
  if (source === 'cron') {
    return {
      kind: 'automated',
      glyph: '⏱',
      label: named(label, task.created_by_automation_name || task.created_by_cron_name),
    }
  }
  if (source.startsWith('trigger:')) return { kind: 'automated', glyph: '⚡', label: named(label, task.created_by_trigger_name) }
  if (source === 'agent') return { kind: 'automated', glyph: '✦', label }
  return HUMAN
}
