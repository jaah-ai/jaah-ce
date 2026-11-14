import { post } from '../../api'
import { useSubmit } from '../../hooks/useSubmit'
import type { ConfirmOptions } from '../ConfirmDialog'
import { KNOWN_TASK_STATES, TRANSITIONS } from '../../generated/taskTransitions'
import type { PipelineTask } from '../../types'
import InlineSelect from '../../ui/InlineSelect'
import { grid } from '../../theme'
import { allowedTransitions } from './controls'
import { orgWaitSentence, stateLabel } from './shared'

/* What each move does, said in the confirm — `routes/tasks/transitions.py::set_task_state` is the behaviour. */
const CONSEQUENCE: Record<string, string> = {
  draft: 'Ends any runner, frees its slot and clears this attempt’s results.',
  ready: 'Queues it for a runner. A live runner is stopped first.',
  active: 'Sets the state only — no runner is started.',
  awaiting_response: 'Sets the state only — no runner is started or asked.',
  paused: 'Ends any live runner; the slot is kept.',
  system_failed: 'Ends any live runner and marks it failed.',
  done: 'Closes it. The outcome is not verified.',
}
const EPIC_CONSEQUENCE: Partial<Record<string, string>> = {
  draft: 'Stops its stage tasks and sets the epic to Draft.',
  ready: 'An epic is never queued \u2014 this starts it.',
}

/* THE STATES THIS ROW MAY MOVE TO NOW: the targets of its `available_transitions` (the server's, else the
 * generated table's — `controls.ts::allowedTransitions`), never the current state. */
export function stateTargets(task: PipelineTask): ReadonlySet<string> {
  const allowed = new Set(allowedTransitions(task))
  return new Set(TRANSITIONS.filter(t => allowed.has(t.name) && t.target !== task.state).map(t => t.target))
}

/* WHETHER THE HOST DRAWS THE CONTROL AT ALL: a transition reaches another state. It gates the control only —
 * once drawn, the control offers every state (an operator override); none, the host draws the read-only badge. */
export function offersStateMove(task: PipelineTask): boolean {
  return stateTargets(task).size > 0
}

/* THE DETAILS TAB'S STATE CONTROL: any state from any state, beyond the Start/Stop/Reset/Complete
 * table. Choosing the current value is a no-op. */
export default function TaskStateControl({
  task,
  confirm,
  onChanged,
  disabled = false,
  inField = false,
}: {
  task: PipelineTask
  confirm: (options: ConfirmOptions) => Promise<boolean>
  onChanged: (updated: PipelineTask) => void
  /** Held while the edit form has unsaved work: the returned row re-keys it. */
  disabled?: boolean
  /** Sits in a labelled `DetailField` cell: the cell's label names it, so the select carries none of its own. It
   *  reads as the current state with a "Change…" action beside it until pressed. */
  inField?: boolean
}) {
  const { busy, formError: error, setFormError: setError, run } = useSubmit()
  const value = task.state
  const isEpic = task.persona === 'epic'
  /* A held ready task's full org-hold sentence rides the select's hover (the header pill names the hold). */
  const title = value === 'ready' && task.wait_reason ? orgWaitSentence(task.wait_reason) : undefined
  const options = KNOWN_TASK_STATES.map(s => ({ value: s, label: stateLabel(s) }))

  async function choose(target: string) {
    if (target === value) return
    const label = stateLabel(target)
    const ok = await confirm({
      title: `Set state to ${label}?`,
      identity: `#${task.id} · ${task.title}`,
      message: (isEpic && EPIC_CONSEQUENCE[target]) || CONSEQUENCE[target] || '',
      confirmLabel: `Set ${label}`,
      destructive: target === 'draft',
    })
    if (!ok) return
    await run('Could not set the state', async () => {
      onChanged(await post<PipelineTask>(`/api/tasks/${task.id}/set-state`, { state: target }))
    })
  }

  /* The field cell reads the current state beside its "Change…" action (`InlineSelect`), so the Summary tab says
     the state without a look back at the header's pill. */
  return (
    <InlineSelect
      label="State"
      testId="task-state"
      rootTestId="task-state-control"
      shown={stateLabel(value)}
      selectTitle={title}
      value={value}
      options={options}
      width={grid(40)}
      onChange={v => void choose(v)}
      inField={inField}
      disabled={disabled}
      busy={busy}
      error={error}
      onDismissError={() => setError('')}
    />
  )
}
