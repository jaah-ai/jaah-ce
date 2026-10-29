import type { TaskStop } from '../../../types'
import { personaLabel } from '../personaRegistry'

/* WHY A STOPPED ROW STOPPED, IN ITS OWN WORDS (AC-6.2/6.3/6.4) — from the server's stop record
 * (`PipelineTask.stop`). Each kind carries its own phrase, so a question, a failure, a pause and a customer
 * wait read apart without colour. An epic placed by a stage names that stage by the child's
 * own persona label (or its title), never a persona literal. */

/** Why the system paused a task on the Jaah account — the row's words after "Paused". */
export const SYSTEM_PAUSE_CAUSE: Record<'balance' | 'jaah_unavailable', string> = {
  balance: 'balance below floor',
  jaah_unavailable: 'Jaah account unavailable',
}

/** The stage a parent was placed by, as its row names it: the child's persona label, else its title. */
function stageName(stop: TaskStop): string | null {
  if (stop.stage_persona) return personaLabel(stop.stage_persona)
  if (stop.stage_title) return stop.stage_title
  // A stage the caller may not read is still a stage.
  return stop.stage_task_id !== null ? 'A stage' : null
}

function clause(text: string | null): string {
  const trimmed = text?.trim() ?? ''
  return trimmed === '' ? '' : trimmed
}

/** "Architect asks: which tenant…", "Failed: gate red", "Paused by Ann", "Paused — balance below floor",
 *  "Waiting on customer — invoice". */
export function stopPhrase(stop: TaskStop): string {
  const stage = stageName(stop)
  const text = clause(stop.text)
  switch (stop.kind) {
    case 'question':
      return `${stage ?? 'Question'}${stage ? ' asks' : ''}${text ? `: ${text}` : ''}`
    case 'failed':
      return `${stage ? `${stage} failed` : 'Failed'}${text ? `: ${text}` : ''}`
    case 'paused':
      return `${stage ? `${stage} paused` : 'Paused'}${stop.by ? ` by ${stop.by}` : ''}`
    case 'balance':
    case 'jaah_unavailable':
      return `${stage ? `${stage} paused` : 'Paused'} — ${SYSTEM_PAUSE_CAUSE[stop.kind]}`
    case 'waiting_on_customer':
      return `${stage ? `${stage}: waiting on customer` : 'Waiting on customer'}${text ? ` — ${text}` : ''}`
  }
}
